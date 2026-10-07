"""Commercial buyer workbook and equivalent CSV result bundle."""

from __future__ import annotations

import io
import zipfile

import pandas as pd

from src.replenishment_engine import DecisionResult


def result_tables(result: DecisionResult, comparison: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """One set of tables feeds both browser totals and downloadable exports."""
    buys = result.buys.copy()
    transfers = result.transfers.copy()
    if len(buys):
        buys["total_cash_required_usd"] = buys["cash_cents"] / 100
        buys["run_id"] = result.run_id
        buys = buys[
            [
                "run_id",
                "sku_id",
                "style_color_size",
                "supplier_id",
                "location",
                "current_on_hand",
                "inbound_on_order",
                "days_of_supply",
                "unconstrained_qty",
                "moq_adjusted_qty",
                "recommended_qty",
                "moq_units",
                "pack_multiple",
                "unit_cost_usd",
                "total_cash_required_usd",
                "expected_arrival",
                "status",
                "reason",
            ]
        ]
    if len(transfers):
        transfers["run_id"] = result.run_id
        transfers = transfers[transfers["unconstrained_qty"] > 0]
        transfers["estimated_transfer_cost_usd"] = (
            transfers["recommended_qty"] * result.scenario.transfer_cost_cents_per_unit / 100
        )
        transfers = transfers[
            [
                "run_id",
                "sku_id",
                "style_color_size",
                "source",
                "destination",
                "current_on_hand",
                "inbound_on_order",
                "days_of_supply",
                "unconstrained_qty",
                "recommended_qty",
                "estimated_transfer_cost_usd",
                "expected_arrival",
                "status",
                "reason",
            ]
        ]
    pending = pd.concat(
        [
            buys.loc[buys["status"].isin(["DEFERRED", "PARTIAL"]), ["sku_id", "location", "status", "reason"]]
            if len(buys)
            else pd.DataFrame(columns=["sku_id", "location", "status", "reason"]),
            transfers.loc[
                transfers["status"] == "PARTIAL_SOURCE_LIMIT", ["sku_id", "destination", "status", "reason"]
            ].rename(columns={"destination": "location"})
            if len(transfers)
            else pd.DataFrame(columns=["sku_id", "location", "status", "reason"]),
            result.review,
        ],
        ignore_index=True,
    )
    summary = pd.DataFrame(
        [
            ("Run ID", result.run_id),
            ("As-of date", str(result.asof)),
            ("Input fingerprint", result.input_fingerprint),
            ("Available new purchasing budget USD", result.scenario.budget_cents / 100),
            ("Recommended new purchase commitments USD", result.spend_cents / 100),
            ("Estimated transfer operating cost USD", result.transfer_cost_cents / 100),
            ("Unspent budget USD", (result.scenario.budget_cents - result.spend_cents) / 100),
            (
                "Purchase lines recommended",
                int((result.buys["recommended_qty"] > 0).sum()) if len(result.buys) else 0,
            ),
            (
                "Purchase lines deferred",
                int((result.buys["status"] == "DEFERRED").sum()) if len(result.buys) else 0,
            ),
            (
                "Transfer lines recommended",
                int((result.transfers["recommended_qty"] > 0).sum()) if len(result.transfers) else 0,
            ),
            ("Items requiring review", len(pending)),
        ],
        columns=["metric", "value"],
    )
    assumptions = pd.DataFrame(
        [
            ("Data", "Synthetic unless uploaded; all recommendations are drafts"),
            ("Budget", "Remaining USD available for new purchase commitments, after existing obligations"),
            (
                "Inventory position",
                "On hand + open supplier supply + dispatched inbound transfers - allocated",
            ),
            ("MOQ", "Minimum positive purchase quantity; pack multiple is a separate rule"),
            ("Transfer lead days", result.scenario.transfer_lead_days),
            ("Transfer operating cost per unit USD", result.scenario.transfer_cost_cents_per_unit / 100),
            ("Supplier lead buffer days", result.scenario.lead_buffer_days),
            ("Service target", result.scenario.service_level),
            ("Policy", result.scenario.policy),
            ("Decision method", "Deterministic greedy heuristic; does not claim mathematical optimality"),
            ("Inventory capital", "Average owned inventory at unit cost; excludes payables and receivables"),
        ],
        columns=["assumption", "value"],
    )
    assumptions["value"] = assumptions["value"].astype(str)
    return {
        "Summary": summary,
        "Purchase Recommendations": buys,
        "Transfer Recommendations": transfers,
        "Deferred Review Items": pending,
        "Scenario Comparison": comparison if comparison is not None else pd.DataFrame(),
        "Assumptions": assumptions,
    }


def excel_bytes(result: DecisionResult, comparison: pd.DataFrame | None = None) -> bytes:
    """Create a readable, filterable purchase and transfer action workbook."""
    buffer = io.BytesIO()
    tables = result_tables(result, comparison)
    with pd.ExcelWriter(
        buffer,
        engine="xlsxwriter",
        engine_kwargs={"options": {"strings_to_formulas": False, "strings_to_urls": False}},
    ) as writer:
        book = writer.book
        header = book.add_format(
            {
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#142B3A",
                "border": 0,
                "text_wrap": True,
                "valign": "vcenter",
            }
        )
        currency = book.add_format({"num_format": '"$"#,##0.00;[Red]("$"#,##0.00)'})
        whole = book.add_format({"num_format": "#,##0"})
        alert = book.add_format({"bg_color": "#FFF0E6", "font_color": "#8A3B00"})
        for sheet, frame in tables.items():
            frame.to_excel(writer, sheet_name=sheet, index=False)
            worksheet = writer.sheets[sheet]
            worksheet.freeze_panes(1, 0)
            worksheet.set_row(0, 32, header)
            worksheet.set_landscape()
            worksheet.fit_to_pages(1, 0)
            worksheet.repeat_rows(0)
            if len(frame) and len(frame.columns):
                worksheet.autofilter(0, 0, len(frame), len(frame.columns) - 1)
            for col_index, col in enumerate(frame.columns):
                width = min(55, max(14, len(str(col)) + 2))
                if col in {"reason", "value"}:
                    width = 52
                worksheet.set_column(col_index, col_index, width)
                if col in {"unit_cost_usd", "total_cash_required_usd"}:
                    worksheet.set_column(col_index, col_index, width, currency)
                elif col.endswith("qty") or col.endswith("units"):
                    worksheet.set_column(col_index, col_index, width, whole)
                elif col == "status" and len(frame):
                    worksheet.conditional_format(
                        1,
                        col_index,
                        len(frame),
                        col_index,
                        {"type": "text", "criteria": "containing", "value": "DEFERRED", "format": alert},
                    )
            worksheet.set_tab_color("#D37642" if "Deferred" in sheet else "#177E89")
    return buffer.getvalue()


def csv_zip_bytes(result: DecisionResult, comparison: pd.DataFrame | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, frame in result_tables(result, comparison).items():
            archive.writestr(name.lower().replace(" ", "_") + ".csv", frame.to_csv(index=False))
    return buffer.getvalue()
