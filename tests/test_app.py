"""Ensure a recruiter can open the demo and change a commercial scenario."""

from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_demo_starts_and_budget_recalculates():
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "Decision brief",
        "Buyer actions",
        "Compare scenarios",
        "Data and method",
    ]
    assert {metric.label: metric.value for metric in app.metric}["Recommended spend"] == "$49,728"
    app.slider[0].set_value(25_000).run(timeout=30)
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Available budget"] == "$25,000"
    assert metrics["Budget remaining"] == "$112"
