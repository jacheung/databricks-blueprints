"""Per-group Prophet functions. Pure pandas, applied per group with Spark `applyInPandas`.

To bring your own model, replace the four model-specific functions: `_fit`, `_forecast`,
`serialize_model` and `deserialize_model`, and its pins in `requirements.txt`. Everything
else here, the notebooks and `router_model.py` are built on those four.
See docs/bring-your-own-model.md."""

from __future__ import annotations

import logging
import time
import zlib
from datetime import datetime, timezone

import numpy as np
import pandas as pd

for _name in ("cmdstanpy", "prophet"):
    logging.getLogger(_name).setLevel(logging.WARNING)

FREQ = "h"  # the synthetic series are hourly

FORECAST_COLUMNS = ["group_name", "ds", "yhat", "yhat_lower", "yhat_upper", "fit_s", "predict_s"]
FORECAST_SCHEMA = ("group_name string, ds string, yhat double, yhat_lower double, yhat_upper double, "
                   "fit_s double, predict_s double")

MODEL_COLUMNS = ["group_name", "model_json", "json_bytes", "trained_at"]
MODEL_SCHEMA = "group_name string, model_json string, json_bytes long, trained_at timestamp"

BACKTEST_COLUMNS = ["group_name", "horizon", "cutoff", "mae", "rmse", "coverage"]
BACKTEST_SCHEMA = ("group_name string, horizon int, cutoff string, mae double, rmse double, "
                   "coverage double")


def source_table_name(n_groups: int, skew: bool) -> str:
    return f"timeseries_{n_groups}_groups" + ("_skew" if skew else "")


def make_group_series(group_name: str, n_rows: int, n_features: int = 2, seed: int | None = None,
                      start: str = "2000-01-01") -> pd.DataFrame:
    """One group's hourly series: ds, group_name, features_*, y."""
    from sklearn.datasets import make_regression

    seed = zlib.crc32(group_name.encode()) if seed is None else seed
    features, target = make_regression(n_samples=n_rows, n_features=n_features, random_state=seed)
    df = pd.DataFrame(features, columns=[f"features_{i}" for i in range(n_features)])
    df.insert(0, "group_name", group_name)
    df.insert(0, "ds", pd.date_range(start=start, periods=n_rows, freq=FREQ))
    df["y"] = target
    return df


def rows_per_group(n_groups: int, mean_rows: int, skew: bool = False, seed: int = 0,
                   min_rows: int = 500) -> list[int]:
    """Rows for each group. Skewed sizes are log-normal with the same mean."""
    if not skew:
        return [mean_rows] * n_groups
    sizes = np.random.default_rng(seed).lognormal(mean=0.0, sigma=1.0, size=n_groups)
    sizes = sizes / sizes.mean() * mean_rows
    return np.maximum(sizes.round(), min_rows).astype(int).tolist()


def _fit(pdf: pd.DataFrame):
    from prophet import Prophet

    history = pd.DataFrame({"ds": pd.to_datetime(pdf["ds"]), "y": pdf["y"]})
    return Prophet(daily_seasonality=True).fit(history)


def _forecast(model, horizon: int) -> pd.DataFrame:
    future = model.make_future_dataframe(periods=horizon, freq=FREQ, include_history=False)
    return model.predict(future)[["ds", "yhat", "yhat_lower", "yhat_upper"]]


def train_and_forecast(pdf: pd.DataFrame, horizon: int = 14) -> pd.DataFrame:
    """Fit one group's model and forecast `horizon` steps. Returns `horizon` rows."""
    group_name = pdf["group_name"].iloc[0]
    t0 = time.perf_counter()
    model = _fit(pdf)
    t1 = time.perf_counter()
    out = _forecast(model, horizon)
    t2 = time.perf_counter()
    out["ds"] = out["ds"].astype(str)
    out.insert(0, "group_name", group_name)
    out["fit_s"] = t1 - t0
    out["predict_s"] = t2 - t1
    return out[FORECAST_COLUMNS]


def backtest(pdf: pd.DataFrame, horizon: int = 14) -> pd.DataFrame:
    """Hold out one group's last `horizon` rows, fit on the rest, and score the forecast
    against them (one row). `coverage` is the share of actuals inside Prophet's default
    80% interval. No MAPE: it blows up when `y` is near zero, as in the synthetic data.

    This is a single window: error at this horizon from one cutoff. To see how stable that
    error is over time, repeat it over several cutoffs (a rolling-origin backtest, e.g.
    `prophet.diagnostics.cross_validation`) and over several horizons."""
    pdf = pdf.sort_values("ds")
    train, test = pdf.iloc[:-horizon], pdf.iloc[-horizon:]
    out = _forecast(_fit(train), horizon)
    actual = test["y"].to_numpy()
    err = out["yhat"].to_numpy() - actual
    inside = (actual >= out["yhat_lower"].to_numpy()) & (actual <= out["yhat_upper"].to_numpy())
    return pd.DataFrame({"group_name": [pdf["group_name"].iloc[0]],
                         "horizon": [horizon],
                         "cutoff": [str(pd.Timestamp(train["ds"].iloc[-1]))],
                         "mae": [float(np.abs(err).mean())],
                         "rmse": [float(np.sqrt((err ** 2).mean()))],
                         "coverage": [float(inside.mean())]})[BACKTEST_COLUMNS]


def slim_for_forecasting(model):
    """Drop what a forward forecast never reads: the training history (kept as its last
    row, which `make_future_dataframe` needs) and Stan's in-sample `trend`. At 50k hourly
    rows this takes the JSON from ~8.3 MB to ~8 KB with identical forecasts. The slim
    model can't predict in-sample, plot history, or cross-validate."""
    model.history = model.history.tail(1)
    model.history_dates = model.history_dates.tail(1)
    model.params = {k: v for k, v in model.params.items() if k != "trend"}
    return model


def serialize_model(model) -> str:
    """A fitted model as the string stored in `model_json`: slim Prophet JSON."""
    from prophet.serialize import model_to_json

    return model_to_json(slim_for_forecasting(model))


def deserialize_model(model_json: str):
    """Inverse of `serialize_model`."""
    from prophet.serialize import model_from_json

    return model_from_json(model_json)


def train_to_json(pdf: pd.DataFrame) -> pd.DataFrame:
    """Fit one group's model and return it as slim Prophet JSON (one row)."""
    model_json = serialize_model(_fit(pdf))
    return pd.DataFrame({"group_name": [pdf["group_name"].iloc[0]],
                         "model_json": [model_json],
                         "json_bytes": [len(model_json.encode())],
                         "trained_at": [datetime.now(timezone.utc).replace(tzinfo=None)]})[MODEL_COLUMNS]


def forecast_from_json(model_json: str, horizon: int) -> dict:
    """Forecast `horizon` steps from a serialized model."""
    return forecast_from_model(deserialize_model(model_json), horizon)


def forecast_from_model(model, horizon: int) -> dict:
    out = _forecast(model, horizon)
    return {"ds": out["ds"].astype(str).tolist(),
            "yhat": out["yhat"].tolist(),
            "yhat_lower": out["yhat_lower"].tolist(),
            "yhat_upper": out["yhat_upper"].tolist()}
