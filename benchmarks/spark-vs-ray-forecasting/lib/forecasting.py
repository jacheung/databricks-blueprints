"""Per-group Prophet train + forecast, copied from solutions/scaling-forecasting. Pure pandas, so the same function runs under Spark
`applyInPandas` and Ray Data `map_groups`."""

from __future__ import annotations

import logging
import time
import zlib

import numpy as np
import pandas as pd

for _name in ("cmdstanpy", "prophet"):
    logging.getLogger(_name).setLevel(logging.WARNING)

FREQ = "h"  # the synthetic series are hourly

FORECAST_COLUMNS = ["group_name", "ds", "yhat", "yhat_lower", "yhat_upper", "fit_s", "predict_s"]
FORECAST_SCHEMA = ("group_name string, ds string, yhat double, yhat_lower double, yhat_upper double, "
                   "fit_s double, predict_s double")


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

    ds = pd.to_datetime(pdf["ds"])
    if ds.dt.tz is not None:  # Ray reads from a SQL warehouse return UTC-aware timestamps
        ds = ds.dt.tz_convert("UTC").dt.tz_localize(None)
    history = pd.DataFrame({"ds": ds, "y": pdf["y"]})
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
