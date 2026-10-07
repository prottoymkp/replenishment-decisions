"""Regenerate the deterministic public demo and its buyer artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import Scenario  # noqa: E402
from src.data_generator import generate  # noqa: E402
from src.data_io import normalize_and_validate, populated_csv_zip, populated_workbook  # noqa: E402
from src.po_exporter import csv_zip_bytes, excel_bytes, result_tables  # noqa: E402
from src.replenishment_engine import decide, prepare_forecaster  # noqa: E402
from src.simulation import compare_presets  # noqa: E402
from src.sql_staging import reconcile_ledger  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs")
    arguments = parser.parse_args()
    generated = generate(arguments.seed)
    public = {name: frame for name, frame in generated.items() if name != "truth_demand"}
    data, issues = normalize_and_validate(public)
    if len(issues):
        raise SystemExit(issues.to_string(index=False))
    ledger = reconcile_ledger(data)
    if len(ledger):
        raise SystemExit(f"Inventory ledger has {len(ledger)} mismatches")
    forecaster = prepare_forecaster(data)
    demo_date = date(2026, 9, 18)
    result = decide(data, Scenario(), demo_date, forecaster)
    comparison = compare_presets(data, generated["truth_demand"], forecaster)
    arguments.output.mkdir(parents=True, exist_ok=True)
    (ROOT / "assets").mkdir(parents=True, exist_ok=True)
    (ROOT / "assets" / "demo_input.xlsx").write_bytes(populated_workbook(data))
    (ROOT / "assets" / "demo_input_csv.zip").write_bytes(populated_csv_zip(data))
    (arguments.output / "draft_buyer_actions.xlsx").write_bytes(excel_bytes(result, comparison))
    (arguments.output / "draft_buyer_actions_csv.zip").write_bytes(csv_zip_bytes(result, comparison))
    comparison.to_csv(ROOT / "assets" / "demo_comparison.csv", index=False)
    forecaster.scores.to_csv(ROOT / "assets" / "forecast_scores.csv", index=False)
    metrics = dict(
        asof=str(demo_date),
        seed=arguments.seed,
        run_id=result.run_id,
        input_fingerprint=result.input_fingerprint,
        budget_usd=result.scenario.budget_cents / 100,
        recommended_spend_usd=result.spend_cents / 100,
        purchase_lines=int((result.buys["recommended_qty"] > 0).sum()),
        deferred_lines=int((result.buys["status"] == "DEFERRED").sum()),
        transfer_lines=int((result.transfers["recommended_qty"] > 0).sum()),
    )
    (ROOT / "assets" / "demo_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    print(result_tables(result, comparison)["Scenario Comparison"].to_string(index=False))


if __name__ == "__main__":
    main()
