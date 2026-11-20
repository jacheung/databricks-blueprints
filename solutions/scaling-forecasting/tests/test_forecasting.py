import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from forecasting import (BACKTEST_COLUMNS, FORECAST_COLUMNS, MODEL_COLUMNS, backtest,  # noqa: E402
                         deserialize_model, forecast_from_json, make_group_series, rows_per_group,
                         serialize_model, source_table_name, train_and_forecast, train_to_json)

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


def test_train_to_json_round_trip_matches_direct_forecast(series):
    row = train_to_json(series)
    assert list(row.columns) == MODEL_COLUMNS
    assert len(row) == 1
    model_json = row.loc[0, "model_json"]
    json.loads(model_json)  # valid JSON
    assert row.loc[0, "json_bytes"] == len(model_json.encode())

    from_json = forecast_from_json(model_json, horizon=HORIZON)
    direct = train_and_forecast(series, horizon=HORIZON)
    assert from_json["ds"] == direct["ds"].tolist()
    assert from_json["yhat"] == pytest.approx(direct["yhat"].tolist(), rel=1e-6)


def test_saved_json_is_slim_and_forecasts_like_full_model():
    """The stored model drops training history and the fitted in-sample trend, which
    forecasting never reads; size must not grow with the number of training rows."""
    from prophet.serialize import model_from_json, model_to_json

    from forecasting import _fit, forecast_from_model

    small = train_to_json(make_group_series("g", n_rows=500, seed=1))
    big_pdf = make_group_series("g", n_rows=5000, seed=1)
    big = train_to_json(big_pdf)
    assert big.loc[0, "json_bytes"] < 20_000
    assert big.loc[0, "json_bytes"] < 1.5 * small.loc[0, "json_bytes"]

    np.random.seed(0)
    full = forecast_from_model(model_from_json(model_to_json(_fit(big_pdf))), HORIZON)
    np.random.seed(0)
    slim = forecast_from_model(model_from_json(big.loc[0, "model_json"]), HORIZON)
    assert slim == full


def test_backtest_holds_out_last_horizon_rows(series):
    from forecasting import _fit, _forecast

    np.random.seed(0)
    row = backtest(series, horizon=HORIZON)
    assert list(row.columns) == BACKTEST_COLUMNS
    assert len(row) == 1
    assert row.loc[0, "group_name"] == "group_01"
    assert row.loc[0, "horizon"] == HORIZON
    # cutoff is the last training timestamp, just before the held-out window
    assert pd.Timestamp(row.loc[0, "cutoff"]) == series["ds"].iloc[-HORIZON - 1]

    train, actual = series.iloc[:-HORIZON], series["y"].iloc[-HORIZON:].to_numpy()
    np.random.seed(0)
    fc = _forecast(_fit(train), HORIZON)
    err = fc["yhat"].to_numpy() - actual
    assert row.loc[0, "mae"] == pytest.approx(np.abs(err).mean())
    assert row.loc[0, "rmse"] == pytest.approx(np.sqrt((err ** 2).mean()))
    inside = (actual >= fc["yhat_lower"].to_numpy()) & (actual <= fc["yhat_upper"].to_numpy())
    assert row.loc[0, "coverage"] == pytest.approx(inside.mean())


def test_backtest_ignores_input_order(series):
    np.random.seed(0)
    ordered = backtest(series, horizon=HORIZON)
    np.random.seed(0)
    shuffled = backtest(series.sample(frac=1, random_state=0), horizon=HORIZON)
    pd.testing.assert_frame_equal(ordered, shuffled)


def test_serialize_round_trip_forecasts_like_the_fitted_model(series):
    from forecasting import _fit, forecast_from_model

    model = _fit(series)
    np.random.seed(0)
    direct = forecast_from_model(model, HORIZON)
    restored = deserialize_model(serialize_model(model))
    np.random.seed(0)
    assert forecast_from_model(restored, HORIZON) == direct


def test_requirements_file_pins_the_installed_prophet():
    import prophet

    reqs = (Path(__file__).resolve().parents[1] / "lib" / "requirements.txt").read_text().split()
    assert f"prophet=={prophet.__version__}" in reqs
