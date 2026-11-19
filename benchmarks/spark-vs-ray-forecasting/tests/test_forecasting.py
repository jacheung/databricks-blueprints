import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from forecasting import (FORECAST_COLUMNS, make_group_series, rows_per_group, source_table_name,  # noqa: E402
                         train_and_forecast)

HORIZON = 14


@pytest.fixture(scope="module")
def series():
    return make_group_series("group_01", n_rows=500, seed=0)


def test_make_group_series_shape(series):
    assert list(series.columns) == ["ds", "group_name", "features_0", "features_1", "y"]
    assert len(series) == 500
    assert series["ds"].diff().dropna().eq(pd.Timedelta(hours=1)).all()
    assert (series["group_name"] == "group_01").all()


def test_make_group_series_is_reproducible():
    a = make_group_series("g", n_rows=50, seed=3)
    b = make_group_series("g", n_rows=50, seed=3)
    pd.testing.assert_frame_equal(a, b)


def test_source_table_name():
    assert source_table_name(10, skew=False) == "timeseries_10_groups"
    assert source_table_name(10000, skew=True) == "timeseries_10000_groups_skew"


def test_rows_per_group_uniform():
    assert rows_per_group(4, mean_rows=100, skew=False) == [100, 100, 100, 100]


def test_rows_per_group_skewed_keeps_total_and_floor():
    rows = rows_per_group(1000, mean_rows=1000, skew=True, seed=0, min_rows=100)
    assert min(rows) >= 100
    assert max(rows) > 3 * min(rows)
    assert abs(sum(rows) - 1000 * 1000) / (1000 * 1000) < 0.05


def test_train_and_forecast_returns_horizon_rows(series):
    out = train_and_forecast(series, horizon=HORIZON)
    assert list(out.columns) == FORECAST_COLUMNS
    assert len(out) == HORIZON
    assert (out["group_name"] == "group_01").all()
    # forecast starts one hour after the last observation
    assert pd.Timestamp(out["ds"].iloc[0]) == series["ds"].max() + pd.Timedelta(hours=1)
    assert (out["fit_s"] > 0).all() and (out["predict_s"] > 0).all()



def test_train_and_forecast_accepts_tz_aware_timestamps(series):
    """Ray's read_databricks_tables returns `ds` as UTC-aware; Prophet rejects timezones."""
    aware = series.assign(ds=series["ds"].dt.tz_localize("UTC"))
    out = train_and_forecast(aware, horizon=HORIZON)
    assert out["ds"].tolist() == train_and_forecast(series, horizon=HORIZON)["ds"].tolist()
