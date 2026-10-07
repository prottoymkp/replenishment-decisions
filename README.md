# Replenishment decisions under lead-time and cash constraints

A working supply-planning portfolio case: one CDC, three stores, 24 footwear SKUs, supplier lead times, minimum quantities, transfers, and a monthly purchasing limit. The system produces **draft** buy, transfer, defer, and review actions. Its public demonstration uses deterministic synthetic data. No simulated result is claimed as a company outcome.

[Open the public case study](https://prottoymkp.github.io/replenishment-decisions/) · [Try the interactive demo](https://replenishment-decisions-mkp.streamlit.app/) · Inspect [decision rules](docs/method.md)

## What a buyer can decide

The prepared 18 September 2026 review has a $50,000 budget for new purchase commitments. The generated decision recommends $49,728 across 11 purchase lines, defers one purchase line, and recommends three feasible CDC-to-store transfers. Open the app to change budget, expected supplier lead time, and safety-stock service target; the action workbook and CSV bundle recalculate from the same result.

The six-month holdout replay (1 April–30 September 2026) uses identical starting stock, latent demand, and supplier conditions for both policies. In the **tight-budget** scenario, the proposed purchasing order fulfills 79.20% of units versus 78.27% for the current-days-of-supply baseline. It loses 142 fewer units, with $216 more purchase commitments, $140.25 more estimated transfer operating cost, and $151.39 higher average inventory value. In the base and supplier-delay scenarios the policies tie. These are modeled trade-offs, not realized savings. See [`assets/demo_comparison.csv`](assets/demo_comparison.csv) for all measured results.

## Quick start

Use Python **3.12**. Create an isolated environment, then:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\run_demo.py
.\.venv\Scripts\python.exe -m streamlit run app.py
```

On macOS/Linux, use `python3.12 -m venv .venv` and `.venv/bin/python` in the same commands. `scripts/run_demo.py` regenerates the buyer workbook and CSV ZIP under `outputs/` and the scenario evidence in `assets/`. It checks input validation and stock reconciliation before calculating. The app opens on the prepared synthetic demonstration without file upload.

For automated checks:

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m pytest -q
```

## Use your own Excel or CSV data

Download the named-sheet workbook or seven CSV templates from **Data and method** in the app. A populated synthetic input workbook is provided as a working example. Complete all seven tables and upload one workbook or the CSV files together. Data stays in the current app session. The public app does not persist uploads or share a mutable database across users.

The first release expects complete daily SKU-store sales and SKU-location inventory snapshots for at least 23 months. A true zero-sale day must be present as a row with `stockout_flag=FALSE`; a missing day is an error. `stockout_flag=TRUE` means sales may be below true demand. The input must also include purchase-order history, receipts and transfer movements so historical as-of positions can be reconstructed. The app shows table, row, field, and corrective action when input is invalid.

[Data dictionary and grain](docs/data_dictionary.md) explains every field and the seven tables. The seed generator lives in [`src/data_generator.py`](src/data_generator.py); `truth_demand` is available only to the offline simulator and never to the decision engine.

## Decision path

1. DuckDB stages the inventory position and reconciles daily stock movements against snapshots.
2. Demand history marks stockout days as censored. A 30-day mean and a weekly seasonal-naive forecast are compared on rolling validation origins. The final six months are reserved for policy replay.
3. The engine recommends feasible CDC-to-store transfers. Supplier purchase candidates use projected lead-time demand, safety stock, MOQ, and pack multiples. Purchases are ranked by recoverable shortage urgency and then modeled margin protected per purchasing dollar.
4. The heuristic commits whole feasible quantities until the monthly cash limit is reached. Unfunded and partially funded lines retain explicit reasons. It does not claim global optimality.
5. Both purchasing policies are replayed with a common weekly transfer rule. The simulation reports fulfilled and lost units, stockout days, purchase commitments, and average owned inventory at unit cost.

Every result includes a run ID, input fingerprint, as-of date, and scenario settings. Estimated transfer operating cost is shown separately from supplier purchasing cash. Purchases and transfers in exports are **draft recommendations**.

## Files and limits

`app.py` is the recruiter interface; `src/` contains the synthetic generator, input validation, SQL staging, forecasting, decision engine, simulation, and export code. `sql/staging.sql` contains the auditable inventory-position query. `tests/` checks ledger, cash, supplier, transfer, leakage, and workbook invariants.

The service target is an approximation derived from forecast-error variation; achieved fill rate must be read from replay. Lost demand in real uploads is unknown and is estimated only from earlier availability evidence. The transfer policy covers CDC-to-store flows. The purchasing allocator is a deterministic greedy heuristic; it can be locally sensible without being globally optimal. Average inventory value excludes payables and receivables and is labeled an inventory-capital measure.

With synthetic data already loaded and the forecaster prepared, one local warm scenario decision took **0.69 seconds** on the implementation machine (Python 3.12, 24 SKUs, four locations). This excludes file parsing and the separate six-month backtest.

## Deployment

The [public case study](https://prottoymkp.github.io/replenishment-decisions/) is hosted from `docs/` through GitHub Pages. The [demo](https://replenishment-decisions-mkp.streamlit.app/) runs `app.py` on Streamlit Community Cloud with Python 3.12 and the root `requirements.txt`. The case-study links are configured in `docs/site-config.json`. CI runs lint, tests, and the reproducible demo. No external credentials or AI model are used by the app.
