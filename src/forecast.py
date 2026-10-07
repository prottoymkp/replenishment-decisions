"""Leakage-controlled forecasts and rolling-origin diagnostics."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, timedelta

import numpy as np
import pandas as pd


def estimate_history(sales: pd.DataFrame) -> pd.DataFrame:
    """Impute censored demand using only earlier uncensored observations."""
    output = []
    for (sku, loc), group in sales.groupby(["sku_id", "location_id"], sort=False):
        available: deque[tuple[date, float]] = deque()
        weekday: dict[int, deque[tuple[date, float]]] = defaultdict(deque)
        for row in group.sort_values("date").itertuples(index=False):
            current = pd.Timestamp(row.date).date()
            while available and (current - available[0][0]).days > 30:
                available.popleft()
            same = weekday[current.weekday()]
            while same and (current - same[0][0]).days > 56:
                same.popleft()
            sold = float(row.units_sold)
            censored = bool(row.stockout_flag)
            if censored:
                evidence = [quantity for _, quantity in same]
                if len(evidence) >= 4:
                    estimate = max(sold, float(np.mean(evidence)))
                    usable = True
                elif len(available) >= 7:
                    estimate = max(sold, float(np.mean([quantity for _, quantity in available])))
                    usable = True
                else:
                    estimate = sold
                    usable = False
            else:
                estimate = sold
                usable = True
                available.append((current, sold))
                same.append((current, sold))
            output.append((current, sku, loc, sold, censored, estimate, usable))
    return pd.DataFrame(
        output,
        columns=[
            "date",
            "sku_id",
            "location_id",
            "units_sold",
            "stockout_flag",
            "estimated_demand",
            "usable",
        ],
    )


def _predict(values: np.ndarray, method: str, horizon: int) -> np.ndarray:
    """Repeat a 30-day mean or the most recent seven weekday values."""
    if len(values) == 0:
        return np.zeros(horizon)
    if method == "SMA30":
        return np.full(horizon, float(np.mean(values[-30:])))
    week = values[-7:]
    if len(week) < 7:
        return np.full(horizon, float(np.mean(values)))
    return np.resize(week, horizon)


class Forecaster:
    """Precomputed as-of estimates; selector is fitted only on the validation window."""

    def __init__(self, history: pd.DataFrame):
        self.history = history.copy()
        self.groups = {
            (sku, loc): frame.sort_values("date").reset_index(drop=True)
            for (sku, loc), frame in history.groupby(["sku_id", "location_id"])
        }
        self.methods: dict[str, str] = {}
        self.sigma: dict[str, float] = {}
        self.scores = pd.DataFrame()

    def fit(self, train_end: date, validation_end: date) -> pd.DataFrame:
        """Score rolling origins entirely before the holdout period."""
        origins = []
        current = train_end + timedelta(days=1)
        while current + timedelta(days=90) <= validation_end:
            origins.append(current)
            current += timedelta(days=7)
        rows = []
        residuals: dict[tuple[str, str], list[float]] = defaultdict(list)
        for (sku, loc), group in self.groups.items():
            dates = np.array(group["date"].tolist(), dtype="datetime64[D]")
            estimates = group["estimated_demand"].to_numpy(float)
            sold = group["units_sold"].to_numpy(float)
            censored = group["stockout_flag"].to_numpy(bool)
            usable = group["usable"].to_numpy(bool)
            for origin in origins:
                start = int(np.searchsorted(dates, np.datetime64(origin)))
                known = estimates[:start]
                if int(usable[:start].sum()) < 30:
                    continue
                for method in ("SMA30", "SEASONAL7"):
                    predicted = _predict(known, method, 90)
                    for horizon in (30, 60, 90):
                        end_date = np.datetime64(origin + timedelta(days=horizon))
                        target_end = int(np.searchsorted(dates, end_date))
                        usable_target = ~censored[start:target_end]
                        actual = sold[start:target_end][usable_target]
                        forecast = predicted[: target_end - start][usable_target]
                        if len(actual):
                            rows.append(
                                (
                                    sku,
                                    loc,
                                    method,
                                    horizon,
                                    len(actual),
                                    float(np.abs(actual - forecast).sum()),
                                    float(actual.sum()),
                                    float((forecast - actual).sum()),
                                )
                            )
            # One-step validation residuals, with origins before each target day.
            valid_idx = np.flatnonzero(
                (dates > np.datetime64(train_end)) & (dates <= np.datetime64(validation_end))
            )
            for target in valid_idx:
                if censored[target] or target < 30:
                    continue
                known = estimates[:target]
                if int(usable[:target].sum()) >= 30:
                    for method in ("SMA30", "SEASONAL7"):
                        residuals[(sku, method)].append(float(sold[target] - _predict(known, method, 1)[0]))
        scores = pd.DataFrame(
            rows,
            columns=[
                "sku_id",
                "location_id",
                "method",
                "horizon",
                "count",
                "absolute_error",
                "actual_units",
                "signed_error",
            ],
        )
        if scores.empty:
            raise ValueError("Insufficient uncensored history for rolling-origin validation")
        pooled = scores.groupby(["sku_id", "method", "horizon"], as_index=False)[
            ["count", "absolute_error", "actual_units", "signed_error"]
        ].sum()
        pooled["mae"] = pooled["absolute_error"] / pooled["count"]
        pooled["wape"] = np.where(
            pooled["actual_units"] > 0, pooled["absolute_error"] / pooled["actual_units"], np.nan
        )
        pooled["bias_units_per_day"] = pooled["signed_error"] / pooled["count"]
        self.scores = pooled
        for sku, group in pooled.groupby("sku_id"):
            ranked = group.groupby("method")["mae"].mean().sort_values(kind="stable")
            self.methods[sku] = str(ranked.index[0])
            self.sigma[sku] = max(0.25, float(np.std(residuals.get((sku, self.methods[sku]), [0.25]))))
        return pooled

    def predict(self, sku: str, loc: str, asof: date, horizon: int) -> np.ndarray:
        frame = self.groups.get((sku, loc))
        if frame is None:
            return np.zeros(horizon)
        known = frame.loc[frame["date"] <= asof, "estimated_demand"].to_numpy(float)
        return _predict(known, self.methods.get(sku, "SMA30"), horizon)

    def evidence(self, sku: str, loc: str, asof: date) -> int:
        frame = self.groups.get((sku, loc))
        if frame is None:
            return 0
        recent_start = asof - timedelta(days=90)
        return int(((frame["date"] <= asof) & (frame["date"] > recent_start) & frame["usable"]).sum())
