"""Benchmark bookkeeping: one result row per run, and the readout table."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

RESULT_COLUMNS = ["engine", "n_groups", "skew", "cores", "partitions", "wall_s", "groups_per_s",
                  "groups_per_s_per_core", "median_fit_s", "median_predict_s", "sum_fit_s", "run_at"]
RESULT_SCHEMA = ("engine string, n_groups int, skew boolean, cores int, partitions int, wall_s double, "
                 "groups_per_s double, groups_per_s_per_core double, median_fit_s double, "
                 "median_predict_s double, sum_fit_s double, run_at timestamp")


def result_row(*, engine: str, n_groups: int, skew: bool, cores: int, partitions: int, wall_s: float,
               forecasts: pd.DataFrame) -> dict:
    """`forecasts` has `horizon` rows per group; each repeats the group's fit_s / predict_s."""
    per_group = forecasts.drop_duplicates("group_name")
    if len(per_group) != n_groups:
        raise ValueError(f"forecasts cover {len(per_group)} of {n_groups} groups")
    return {"engine": engine, "n_groups": n_groups, "skew": skew, "cores": cores, "partitions": partitions,
            "wall_s": wall_s,
            "groups_per_s": n_groups / wall_s,
            "groups_per_s_per_core": n_groups / wall_s / cores,
            "median_fit_s": float(per_group["fit_s"].median()),
            "median_predict_s": float(per_group["predict_s"].median()),
            "sum_fit_s": float(per_group["fit_s"].sum()),
            "run_at": datetime.now(timezone.utc).replace(tzinfo=None)}


def resolve_partitions(partitions: int, n_groups: int) -> int:
    """0 means one partition per group, so each task fits (on average) one group."""
    return partitions or n_groups


def _fmt(row, col: str, spec: str) -> str:
    return "—" if row is None else format(row[col], spec)


def readout_markdown(results: pd.DataFrame) -> str:
    """Latest run per (n_groups, skew, partitions, engine), Spark vs Ray side by side."""
    keys = ["n_groups", "skew", "partitions"]
    latest = (results.sort_values("run_at").groupby(keys + ["engine"]).tail(1)
                     .set_index(keys + ["engine"]))
    lines = ["| Groups | Sizes | Partitions | Spark wall (s) | Ray wall (s) | Ray speedup | Spark groups/s/core | "
             "Ray groups/s/core | Median fit (s) |",
             "|---:|---|---:|---:|---:|---:|---:|---:|---:|"]
    for n_groups, skew, partitions in sorted({k[:3] for k in latest.index}):
        cell = latest.loc[(n_groups, skew, partitions)]
        spark, ray = (cell.loc[e] if e in cell.index else None for e in ("spark", "ray"))
        speedup = f"{spark['wall_s'] / ray['wall_s']:.2f}×" if spark is not None and ray is not None else "—"
        fit = (spark if spark is not None else ray)["median_fit_s"]
        lines.append(f"| {n_groups} | {'skewed' if skew else 'uniform'} | {partitions} | "
                     f"{_fmt(spark, 'wall_s', '.1f')} | {_fmt(ray, 'wall_s', '.1f')} | {speedup} | "
                     f"{_fmt(spark, 'groups_per_s_per_core', '.3f')} | {_fmt(ray, 'groups_per_s_per_core', '.3f')} | "
                     f"{fit:.2f} |")
    return "\n".join(lines)
