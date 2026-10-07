"""Template-based CSV/Excel input with row-level validation."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pandas as pd

from src.data_generator import TABLES

SCHEMA = {
    "dim_items": [
        "sku_id",
        "style",
        "color",
        "size",
        "category",
        "supplier_id",
        "unit_cost_usd",
        "wholesale_price_usd",
        "moq_units",
        "pack_multiple",
        "supplier_lead_time_days",
    ],
    "dim_locations": ["location_id", "location_name", "location_type"],
    "fact_sales": ["date", "sku_id", "location_id", "units_sold", "stockout_flag"],
    "fact_inventory_snapshot": ["date", "sku_id", "location_id", "on_hand_units", "allocated_units"],
    "fact_open_purchase_orders": [
        "po_id",
        "sku_id",
        "destination_loc",
        "order_date",
        "expected_delivery_date",
        "qty_ordered",
        "qty_cancelled",
        "cancellation_date",
    ],
    "fact_movements": ["date", "sku_id", "location_id", "movement_type", "delta_units", "reference_id"],
    "fact_transfers": [
        "transfer_id",
        "sku_id",
        "source_loc",
        "destination_loc",
        "dispatch_date",
        "expected_arrival_date",
        "qty_dispatched",
    ],
}
DATES = {
    "date",
    "order_date",
    "expected_delivery_date",
    "cancellation_date",
    "dispatch_date",
    "expected_arrival_date",
}
INTEGERS = {
    "units_sold",
    "on_hand_units",
    "allocated_units",
    "qty_ordered",
    "qty_cancelled",
    "qty_dispatched",
    "delta_units",
    "moq_units",
    "pack_multiple",
    "supplier_lead_time_days",
}
MONEY = {"unit_cost_usd", "wholesale_price_usd"}
KEYS = {
    "dim_items": ["sku_id"],
    "dim_locations": ["location_id"],
    "fact_sales": ["date", "sku_id", "location_id"],
    "fact_inventory_snapshot": ["date", "sku_id", "location_id"],
    "fact_open_purchase_orders": ["po_id"],
    "fact_transfers": ["transfer_id"],
}


def template_workbook() -> bytes:
    """Downloadable named-sheet Excel template."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        for name, columns in SCHEMA.items():
            pd.DataFrame(columns=columns).to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


def template_csv_zip() -> bytes:
    """Downloadable UTF-8 CSV templates."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, columns in SCHEMA.items():
            archive.writestr(f"{name}.csv", pd.DataFrame(columns=columns).to_csv(index=False))
    return buffer.getvalue()


def populated_workbook(data: dict[str, pd.DataFrame]) -> bytes:
    """Create a seven-sheet synthetic example that can be re-uploaded unchanged."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        for name in TABLES:
            data[name][SCHEMA[name]].to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


def populated_csv_zip(data: dict[str, pd.DataFrame]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in TABLES:
            archive.writestr(f"{name}.csv", data[name][SCHEMA[name]].to_csv(index=False))
    return buffer.getvalue()


def read_csv_directory(path: str | Path) -> dict[str, pd.DataFrame]:
    path = Path(path)
    missing = [name for name in TABLES if not (path / f"{name}.csv").exists()]
    if missing:
        raise ValueError(f"Missing CSV files: {', '.join(missing)}")
    return {
        name: pd.read_csv(path / f"{name}.csv", dtype={"sku_id": "string", "size": "string"})
        for name in TABLES
    }


def read_xlsx(upload: bytes | io.BytesIO) -> dict[str, pd.DataFrame]:
    source = io.BytesIO(upload) if isinstance(upload, bytes) else upload
    workbook = pd.ExcelFile(source, engine="openpyxl")
    missing = [name for name in TABLES if name not in workbook.sheet_names]
    if missing:
        raise ValueError(f"Missing Excel sheets: {', '.join(missing)}")
    return {
        name: pd.read_excel(workbook, sheet_name=name, dtype={"sku_id": "string", "size": "string"})
        for name in TABLES
    }


def _issue(table: str, row: int | str, field: str, problem: str, action: str) -> dict[str, object]:
    return dict(table=table, row=row, field=field, problem=problem, action=action)


def normalize_and_validate(raw: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Return typed copies and actionable findings; no input is silently repaired."""
    issues: list[dict[str, object]] = []
    data: dict[str, pd.DataFrame] = {}
    for name, columns in SCHEMA.items():
        if name not in raw:
            issues.append(_issue(name, "file", "all", "Missing table", "Provide the named sheet or CSV"))
            continue
        frame = raw[name].copy()
        missing = [column for column in columns if column not in frame.columns]
        for column in missing:
            issues.append(
                _issue(name, "header", column, "Missing column", "Use the supplied template header")
            )
        if missing:
            continue
        for column in columns:
            if column in DATES:
                parsed = pd.to_datetime(frame[column], format="%Y-%m-%d", errors="coerce")
                for idx in frame.index[frame[column].notna() & parsed.isna()]:
                    issues.append(_issue(name, int(idx) + 2, column, "Invalid date", "Use YYYY-MM-DD"))
                frame[column] = parsed.dt.date
            elif column in INTEGERS | MONEY:
                parsed = pd.to_numeric(frame[column], errors="coerce")
                bad = frame[column].notna() & (
                    parsed.isna() | (parsed % 1 != 0 if column in INTEGERS else False)
                )
                for idx in frame.index[bad]:
                    issues.append(
                        _issue(name, int(idx) + 2, column, "Invalid number", "Enter a valid numeric value")
                    )
                frame[column] = parsed
            elif column == "stockout_flag":
                mapping = {"true": True, "false": False, "1": True, "0": False}
                values = frame[column].astype(str).str.lower().map(mapping)
                for idx in frame.index[values.isna()]:
                    issues.append(
                        _issue(
                            name,
                            int(idx) + 2,
                            column,
                            "Missing or invalid availability",
                            "Enter TRUE or FALSE; do not infer from sales",
                        )
                    )
                frame[column] = values
            else:
                frame[column] = frame[column].astype("string").str.strip()
                if column not in {"reference_id", "size", "style", "color", "category", "supplier_id"}:
                    for idx in frame.index[frame[column].isna() | (frame[column] == "")]:
                        issues.append(_issue(name, int(idx) + 2, column, "Missing identifier", "Fill the ID"))
        for column in columns:
            if column == "cancellation_date" or column == "reference_id":
                continue
            if column in DATES and frame[column].isna().any():
                for idx in frame.index[frame[column].isna()]:
                    issues.append(_issue(name, int(idx) + 2, column, "Missing date", "Enter YYYY-MM-DD"))
            if column in INTEGERS | MONEY and frame[column].isna().any():
                for idx in frame.index[frame[column].isna()]:
                    issues.append(
                        _issue(name, int(idx) + 2, column, "Missing number", "Fill the required value")
                    )
            if column in MONEY:
                too_precise = frame[column].notna() & (
                    (frame[column] * 100).round() - frame[column] * 100
                ).abs().gt(1e-6)
                for idx in frame.index[too_precise]:
                    issues.append(
                        _issue(
                            name,
                            int(idx) + 2,
                            column,
                            "Too many decimal places",
                            "Use USD amounts with at most two decimals",
                        )
                    )
        if name in KEYS:
            duplicate = frame.duplicated(KEYS[name], keep=False)
            for idx in frame.index[duplicate]:
                issues.append(
                    _issue(name, int(idx) + 2, ", ".join(KEYS[name]), "Duplicate key", "Keep one row per key")
                )
        data[name] = frame
    if len(data) == len(SCHEMA):
        sku_set = set(data["dim_items"]["sku_id"].dropna())
        loc_set = set(data["dim_locations"]["location_id"].dropna())
        for name, frame in data.items():
            if "sku_id" in frame:
                for idx in frame.index[~frame["sku_id"].isin(sku_set)]:
                    issues.append(_issue(name, int(idx) + 2, "sku_id", "Orphan SKU", "Add it to dim_items"))
            for column in ("location_id", "destination_loc", "source_loc"):
                if column in frame:
                    for idx in frame.index[~frame[column].isin(loc_set)]:
                        issues.append(
                            _issue(name, int(idx) + 2, column, "Orphan location", "Add it to dim_locations")
                        )
        for column in ("unit_cost_usd", "wholesale_price_usd"):
            for idx in data["dim_items"].index[
                data["dim_items"][column].isna() | (data["dim_items"][column] <= 0)
            ]:
                issues.append(
                    _issue(
                        "dim_items", int(idx) + 2, column, "Missing or nonpositive price", "Enter USD price"
                    )
                )
        for column in ("moq_units", "pack_multiple", "supplier_lead_time_days"):
            for idx in data["dim_items"].index[
                data["dim_items"][column].isna() | (data["dim_items"][column] <= 0)
            ]:
                issues.append(
                    _issue(
                        "dim_items", int(idx) + 2, column, "Invalid supplier rule", "Enter positive integer"
                    )
                )
        if data["fact_inventory_snapshot"]["date"].notna().any():
            first = min(data["fact_inventory_snapshot"]["date"].dropna())
            last = max(data["fact_inventory_snapshot"]["date"].dropna())
            calendar = pd.date_range(first, last, freq="D").date
            expected_stock = pd.MultiIndex.from_product(
                [calendar, sku_set, loc_set], names=["date", "sku_id", "location_id"]
            )
            actual_stock = pd.MultiIndex.from_frame(
                data["fact_inventory_snapshot"][["date", "sku_id", "location_id"]]
            )
            missing_stock = expected_stock.difference(actual_stock)
            for day, sku, loc in missing_stock[:100]:
                issues.append(
                    _issue(
                        "fact_inventory_snapshot",
                        str(day),
                        f"{sku}/{loc}",
                        "Missing daily snapshot",
                        "Add a dated zero-stock row if the location was open",
                    )
                )
            store_set = set(
                data["dim_locations"].loc[data["dim_locations"]["location_type"] == "Store", "location_id"]
            )
            expected_sales = pd.MultiIndex.from_product(
                [calendar, sku_set, store_set], names=["date", "sku_id", "location_id"]
            )
            actual_sales = pd.MultiIndex.from_frame(data["fact_sales"][["date", "sku_id", "location_id"]])
            missing_sales = expected_sales.difference(actual_sales)
            for day, sku, loc in missing_sales[:100]:
                issues.append(
                    _issue(
                        "fact_sales",
                        str(day),
                        f"{sku}/{loc}",
                        "Missing sales observation",
                        "Add a recorded zero-sale row with an availability flag",
                    )
                )
        orders = data["fact_open_purchase_orders"]
        for idx in orders.index[orders["qty_cancelled"] > orders["qty_ordered"]]:
            issues.append(
                _issue(
                    "fact_open_purchase_orders",
                    int(idx) + 2,
                    "qty_cancelled",
                    "Cancellation exceeds order",
                    "Correct ordered or cancelled units",
                )
            )
        transfers = data["fact_transfers"]
        for idx in transfers.index[transfers["source_loc"] == transfers["destination_loc"]]:
            issues.append(
                _issue(
                    "fact_transfers",
                    int(idx) + 2,
                    "destination_loc",
                    "Transfer has no movement",
                    "Choose a different destination",
                )
            )
        for name, column in (
            ("fact_sales", "units_sold"),
            ("fact_inventory_snapshot", "on_hand_units"),
            ("fact_inventory_snapshot", "allocated_units"),
            ("fact_open_purchase_orders", "qty_ordered"),
            ("fact_open_purchase_orders", "qty_cancelled"),
            ("fact_transfers", "qty_dispatched"),
        ):
            for idx in data[name].index[data[name][column].fillna(-1) < 0]:
                issues.append(
                    _issue(name, int(idx) + 2, column, "Negative quantity", "Enter nonnegative units")
                )
        bad_alloc = (
            data["fact_inventory_snapshot"]["allocated_units"]
            > data["fact_inventory_snapshot"]["on_hand_units"]
        )
        for idx in data["fact_inventory_snapshot"].index[bad_alloc]:
            issues.append(
                _issue(
                    "fact_inventory_snapshot",
                    int(idx) + 2,
                    "allocated_units",
                    "Allocation exceeds stock",
                    "Correct reserved and on-hand units",
                )
            )
        locations = data["dim_locations"]
        if (locations["location_type"] == "CDC").sum() != 1:
            issues.append(
                _issue("dim_locations", "table", "location_type", "Exactly one CDC required", "Mark one CDC")
            )
    return data, pd.DataFrame(issues, columns=["table", "row", "field", "problem", "action"])


def fingerprint(data: dict[str, pd.DataFrame]) -> str:
    digest = hashlib.sha256()
    for name in TABLES:
        digest.update(name.encode())
        digest.update(data[name].to_csv(index=False, lineterminator="\n").encode())
    return digest.hexdigest()[:16]

