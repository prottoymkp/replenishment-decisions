"""Buyer review control tower for synthetic or template-based retail data."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from src.config import Scenario
from src.data_generator import generate
from src.data_io import normalize_and_validate, read_xlsx, template_csv_zip, template_workbook
from src.po_exporter import csv_zip_bytes, excel_bytes, result_tables
from src.replenishment_engine import decide, prepare_forecaster
from src.sql_staging import reconcile_ledger

ROOT = Path(__file__).resolve().parent
st.set_page_config(
    page_title="Replenishment decisions | Portfolio case",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(show_spinner=False)
def demo_data() -> dict[str, pd.DataFrame]:
    return {name: frame for name, frame in generate(42).items() if name != "truth_demand"}


@st.cache_resource(show_spinner=False)
def demo_forecaster():
    return prepare_forecaster(demo_data())


def load_uploaded_data() -> dict[str, pd.DataFrame] | None:
    st.sidebar.markdown("**Import your data**")
    format_choice = st.sidebar.radio("File format", ["Excel workbook", "CSV files"], horizontal=True)
    try:
        if format_choice == "Excel workbook":
            file = st.sidebar.file_uploader("Named-sheet .xlsx workbook", type="xlsx")
            return read_xlsx(file.getvalue()) if file else None
        files = st.sidebar.file_uploader("Seven named .csv files", type="csv", accept_multiple_files=True)
        if not files:
            return None
        return {
            file.name.removesuffix(".csv"): pd.read_csv(file, dtype={"sku_id": "string", "size": "string"})
            for file in files
        }
    except Exception as exc:
        st.error(f"Could not read the upload: {exc}")
        return None


def action_table(result) -> pd.DataFrame:
    labels = {
        "ADJUSTED_MOQ_PACK": "Raised to the supplier minimum and pack size",
        "UNAVOIDABLE_BEFORE_ARRIVAL": "Some shortage happens before the new order can arrive",
        "DEFERRED_CASH_LIMIT": "Deferred because the remaining budget cannot fund a valid minimum order",
        "PARTIAL_CASH_LIMIT": "Partly funded within the available budget",
    }

    def explain(value: object) -> str:
        return "; ".join(labels.get(code.strip(), code.strip()) for code in str(value).split(", "))

    rows = []
    for row in result.buys.itertuples(index=False):
        rows.append(
            dict(
                action="BUY" if row.recommended_qty else "DEFER",
                sku_id=row.sku_id,
                location=row.location,
                status=row.status,
                recommended_qty=row.recommended_qty,
                cash_usd=row.cash_cents / 100,
                transfer_cost_usd=0,
                days_of_supply=row.days_of_supply,
                reason=explain(row.reason),
            )
        )
    for row in result.transfers.itertuples(index=False):
        if row.unconstrained_qty:
            rows.append(
                dict(
                    action="TRANSFER" if row.recommended_qty else "REVIEW",
                    sku_id=row.sku_id,
                    location=row.destination,
                    status=row.status,
                    recommended_qty=row.recommended_qty,
                    cash_usd=0,
                    transfer_cost_usd=row.recommended_qty
                    * result.scenario.transfer_cost_cents_per_unit
                    / 100,
                    days_of_supply=row.days_of_supply,
                    reason=explain(row.reason),
                )
            )
    for row in result.review.itertuples(index=False):
        rows.append(
            dict(
                action="REVIEW",
                sku_id=row.sku_id,
                location=row.location,
                status=row.status,
                recommended_qty=0,
                cash_usd=0,
                transfer_cost_usd=0,
                days_of_supply=None,
                reason=explain(row.reason),
            )
        )
    return pd.DataFrame(
        rows,
        columns=[
            "action",
            "sku_id",
            "location",
            "status",
            "recommended_qty",
            "cash_usd",
            "transfer_cost_usd",
            "days_of_supply",
            "reason",
        ],
    )


st.title("Replenishment decisions")
st.write(
    "Decide what to buy, transfer, or defer across one distribution center and three stores. "
    "The budget is finite; the shortage exposure stays visible."
)
source = st.sidebar.radio("Data source", ["Prepared demonstration", "Use your own data"])
st.caption(
    "Synthetic footwear retailer · six-month simulated policy evidence · all buyer files are drafts"
    if source == "Prepared demonstration"
    else "Your uploaded data · as-of recommendations and validation · all buyer files are drafts"
)
budget = st.sidebar.slider(
    "Purchasing budget available now · USD",
    0,
    100_000,
    50_000,
    step=1_000,
    help="Funds remaining for new supplier purchase commitments, after existing obligations.",
)
lead_buffer = st.sidebar.slider(
    "Supplier lead-time adjustment · days",
    -15,
    30,
    0,
    help="Adjust expected supplier time for new purchase recommendations.",
)
service = st.sidebar.slider(
    "Service target",
    85,
    98,
    95,
    step=1,
    format="%d%%",
    help="Used for safety stock. Actual achieved service is reported by simulation.",
)
scenario = Scenario(budget_cents=budget * 100, lead_buffer_days=lead_buffer, service_level=service / 100)

if source == "Prepared demonstration":
    data = demo_data()
    asof = st.sidebar.selectbox(
        "Review date",
        [date(2026, 9, 18), date(2026, 4, 30), date(2026, 8, 31)],
        format_func=lambda value: value.strftime("%d %B %Y"),
    )
    model = demo_forecaster()
    comparison_path = ROOT / "assets" / "demo_comparison.csv"
    comparison = pd.read_csv(comparison_path) if comparison_path.exists() else pd.DataFrame()
else:
    data = load_uploaded_data()
    asof = None
    comparison = pd.DataFrame()

if data is None:
    st.info(
        "Upload the completed template to calculate decisions. The prepared demonstration is ready without an upload."
    )
    st.download_button(
        "Download Excel input template", template_workbook(), "replenishment_input_template.xlsx"
    )
    st.download_button("Download CSV input templates", template_csv_zip(), "replenishment_csv_templates.zip")
    st.stop()

with st.spinner("Checking input and calculating decisions…"):
    typed, issues = normalize_and_validate(data)
    if len(issues):
        st.error(f"Input needs correction: {len(issues)} finding(s). No recommendations were generated.")
        st.dataframe(issues, hide_index=True, width="stretch")
        st.stop()
    ledger = reconcile_ledger(typed)
    if len(ledger):
        st.error(
            f"Inventory snapshots disagree with movements on {len(ledger)} row(s). No recommendations were generated."
        )
        st.dataframe(ledger.head(100), hide_index=True, width="stretch")
        st.stop()
    try:
        if source != "Prepared demonstration":
            asof = max(typed["fact_inventory_snapshot"]["date"])
            model = prepare_forecaster(typed)
        result = decide(typed, scenario, asof, model)
    except Exception as exc:
        st.error(f"Planning stopped: {exc}")
        st.stop()

tables = result_tables(result, comparison)
actions = action_table(result)
brief, buyer, scenarios, method = st.tabs(
    ["Decision brief", "Buyer actions", "Compare scenarios", "Data and method"]
)

with brief:
    st.subheader("Today's allocation")
    cols = st.columns([1.6, 1, 1])
    cols[0].metric("Recommended spend", f"${result.spend_cents / 100:,.0f}")
    cols[1].metric("Available budget", f"${budget:,.0f}")
    cols[2].metric("Budget remaining", f"${(scenario.budget_cents - result.spend_cents) / 100:,.0f}")
    exposed = int((result.buys["status"] == "DEFERRED").sum()) if len(result.buys) else 0
    st.progress(result.spend_cents / scenario.budget_cents if scenario.budget_cents else 0)
    st.caption("Share of available funds committed to new supplier purchases. Unspent budget is not savings.")
    st.divider()
    left, right = st.columns([1.8, 1], gap="large")
    with left:
        st.markdown("#### Buyer queue")
        prioritized = pd.concat(
            [actions[actions["action"] == kind].head(1) for kind in ("BUY", "TRANSFER", "DEFER", "REVIEW")],
            ignore_index=True,
        ).head(3)
        if prioritized.empty:
            st.success("No immediate purchase or transfer trigger at this review date.")
        for row in prioritized.itertuples(index=False):
            with st.container(border=True):
                st.markdown(f"**{row.action}** · {row.sku_id} → {row.location}")
                st.write(f"{row.recommended_qty:,.0f} units · {row.reason}")
    with right:
        st.markdown("#### Exposure")
        st.metric("Purchase lines deferred", exposed)
        st.write(
            "A deferred order remains a service risk. Open **Buyer actions** to see the SKU, "
            "reason, and inventory projection before committing a draft order."
        )
    with st.expander("How this recommendation was made"):
        st.write(
            "The planner checks current and inbound stock by date, protects store coverage with available CDC transfers, "
            "then ranks feasible supplier purchases under the cash cap. It respects minimum order quantities and pack multiples. "
            "An order arriving after a shortage is flagged; the recommendation does not imply that earlier lost demand can be recovered."
        )

with buyer:
    st.subheader("Draft buyer actions")
    filters = st.multiselect(
        "Show actions", ["BUY", "TRANSFER", "DEFER", "REVIEW"], default=["BUY", "TRANSFER", "DEFER", "REVIEW"]
    )
    display = actions[actions["action"].isin(filters)]
    st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        column_config={
            "recommended_qty": st.column_config.NumberColumn("Qty", format="%d"),
            "cash_usd": st.column_config.NumberColumn("New cash · USD", format="$%.2f"),
            "transfer_cost_usd": st.column_config.NumberColumn("Transfer cost · USD", format="$%.2f"),
            "days_of_supply": st.column_config.NumberColumn("Days cover", format="%.1f"),
        },
    )
    if len(result.timeline):
        selected = st.selectbox("Trace a SKU", sorted(result.timeline["sku_id"].unique()))
        lines = result.timeline[result.timeline["sku_id"] == selected]
        chart = px.line(
            lines,
            x="date",
            y="projected_units",
            color="location",
            markers=True,
            labels={"projected_units": "Projected free units", "date": "Date"},
        )
        chart.add_hline(y=0, line_dash="dash", line_color="#A76537")
        chart.update_layout(margin=dict(l=5, r=5, t=12, b=5), height=360)
        st.plotly_chart(
            chart,
            width="stretch",
            alt=f"Projected free inventory by location over time for SKU {selected}; the dashed line marks zero stock.",
        )
        st.caption(
            "Illustrative projection from known stock, open transfers, and selected forecast. Incoming proposed actions are listed in the table."
        )
    st.markdown("**Download buyer files**")
    d1, d2 = st.columns(2)
    d1.download_button(
        "Draft Excel action workbook",
        excel_bytes(result, comparison),
        file_name=f"buyer_actions_{result.run_id}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    d2.download_button(
        "All result tables as CSV ZIP",
        csv_zip_bytes(result, comparison),
        file_name=f"buyer_actions_{result.run_id}.zip",
        mime="application/zip",
    )

with scenarios:
    st.subheader("Six-month simulation · 1 April–30 September 2026")
    st.caption(
        "Generated true demand is used to score outcomes only. Both policies face the same demand, starting stock, and supplier conditions. "
        "Scenario results use the prepared demonstration and are recalculated offline, separately from the current review controls."
    )
    if comparison.empty:
        st.info(
            "Replay evidence is available in the prepared demonstration after the demo artifacts are generated."
        )
    else:
        scenario_name = st.radio("Scenario", ["Base case", "Tight budget", "Supplier delay"], horizontal=True)
        subset = comparison[comparison["scenario"] == scenario_name].copy()
        st.dataframe(
            subset[
                [
                    "policy",
                    "budget_usd",
                    "supplier_delay_days",
                    "unit_fill_rate",
                    "lost_units",
                    "stockout_sku_store_days",
                    "purchase_commitments_usd",
                    "transfer_operating_cost_usd",
                    "average_inventory_value_usd",
                ]
            ],
            hide_index=True,
            width="stretch",
        )
        metric = st.selectbox(
            "Compare",
            ["unit_fill_rate", "lost_units", "average_inventory_value_usd", "purchase_commitments_usd"],
        )
        fig = px.bar(
            subset,
            x="policy",
            y=metric,
            color="policy",
            color_discrete_map={"baseline": "#8C9690", "proposed": "#165A54"},
            text_auto=True,
        )
        fig.update_layout(
            showlegend=False, margin=dict(l=5, r=5, t=10, b=5), height=350, xaxis_title="Policy"
        )
        st.plotly_chart(
            fig,
            width="stretch",
            alt=f"{metric.replace('_', ' ')} comparison for the baseline and proposed policies in {scenario_name}.",
        )
        st.caption(
            "The proposed policy uses preventable shortage urgency and margin protected per purchasing dollar. "
            "The baseline purchases by current days of supply. Average inventory value is an inventory-capital measure."
        )

with method:
    st.subheader("Data, checks, and method")
    st.write(
        f"**Input fingerprint:** `{result.input_fingerprint}` · **Run ID:** `{result.run_id}` · "
        f"**As-of date:** {result.asof}"
    )
    st.success("Input schema and daily movement ledger reconciled.")
    st.markdown("**Import templates**")
    t1, t2 = st.columns(2)
    t1.download_button(
        "Excel template · seven sheets", template_workbook(), "replenishment_input_template.xlsx"
    )
    t2.download_button("CSV templates · ZIP", template_csv_zip(), "replenishment_csv_templates.zip")
    if source == "Prepared demonstration" and (ROOT / "assets" / "demo_input.xlsx").exists():
        st.download_button(
            "Populated synthetic input workbook",
            (ROOT / "assets" / "demo_input.xlsx").read_bytes(),
            "synthetic_replenishment_input.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    st.write(
        "Daily sales need an explicit stockout flag. Missing dates must be corrected before a run; a zero-sale day with stock available is valid zero demand."
    )
    with st.expander("Forecast validation"):
        score = result.forecast_scores
        if len(score):
            pooled = score.groupby(["method", "horizon"], as_index=False)[
                ["absolute_error", "actual_units", "signed_error", "count"]
            ].sum()
            pooled["MAE"] = pooled["absolute_error"] / pooled["count"]
            pooled["WAPE"] = pooled["absolute_error"] / pooled["actual_units"].replace(0, float("nan"))
            pooled["Bias/day"] = pooled["signed_error"] / pooled["count"]
            st.dataframe(
                pooled[["method", "horizon", "MAE", "WAPE", "Bias/day", "count"]],
                hide_index=True,
                width="stretch",
            )
    with st.expander("Decision rules and limitations"):
        st.write(
            "Supplier buys enter the CDC; transfers can move only available CDC stock to stores. The budget caps new supplier "
            "commitments. Minimum order quantity and pack multiple are separate constraints. Allocation is deterministic and explainable, "
            "but it is not a global mathematical optimum. Safety stock approximates forecast uncertainty; its selected service target "
            "is not a promise of achieved service. The synthetic replay demonstrates modeled trade-offs, not realized company savings."
        )
    st.dataframe(tables["Assumptions"], hide_index=True, width="stretch")
