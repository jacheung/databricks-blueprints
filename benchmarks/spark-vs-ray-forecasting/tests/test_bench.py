import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from bench import RESULT_COLUMNS, resolve_partitions, readout_markdown, result_row  # noqa: E402


def forecasts(groups, fit_s, horizon=3):
    """Forecast output: `horizon` rows per group, each repeating its group's timings."""
    return pd.DataFrame([{"group_name": g, "fit_s": f, "predict_s": f / 10}
                         for g, f in zip(groups, fit_s) for _ in range(horizon)])


def test_result_row_counts_each_group_once():
    row = result_row(engine="spark", n_groups=4, skew=False, cores=2, partitions=8, wall_s=8.0,
                     forecasts=forecasts(["a", "b", "c", "d"], [1.0, 2.0, 3.0, 10.0]))
    assert set(row) == set(RESULT_COLUMNS)
    assert row["groups_per_s"] == pytest.approx(0.5)
    assert row["groups_per_s_per_core"] == pytest.approx(0.25)
    assert row["median_fit_s"] == pytest.approx(2.5)
    assert row["median_predict_s"] == pytest.approx(0.25)
    assert row["sum_fit_s"] == pytest.approx(16.0)


def test_result_row_rejects_missing_groups():
    with pytest.raises(ValueError, match="3 of 4 groups"):
        result_row(engine="ray", n_groups=4, skew=False, cores=2, partitions=8, wall_s=1.0,
                   forecasts=forecasts(["a", "b", "c"], [1.0, 1.0, 1.0]))


def test_readout_compares_latest_run_per_cell():
    def row(engine, wall_s, run_at, skew=False, partitions=1):
        r = result_row(engine=engine, n_groups=2, skew=skew, cores=1, partitions=partitions, wall_s=wall_s,
                       forecasts=forecasts(["a", "b"], [1.0, 1.0]))
        r["run_at"] = pd.Timestamp(run_at)
        return r

    results = pd.DataFrame([row("spark", 100.0, "2026-01-01"),
                            row("spark", 40.0, "2026-01-02"),  # latest spark run wins
                            row("ray", 20.0, "2026-01-02")])
    md = readout_markdown(results)
    lines = [ln for ln in md.splitlines() if ln.startswith("| 2 ")]
    assert len(lines) == 1
    assert "40.0" in lines[0] and "20.0" in lines[0] and "100.0" not in lines[0]
    assert "2.00×" in lines[0]  # spark wall / ray wall


def test_readout_keeps_partition_counts_apart():
    def row(engine, wall_s, partitions):
        return result_row(engine=engine, n_groups=2, skew=True, cores=1, partitions=partitions, wall_s=wall_s,
                          forecasts=forecasts(["a", "b"], [1.0, 1.0]))

    md = readout_markdown(pd.DataFrame([row("spark", 10.0, 1), row("ray", 5.0, 1),
                                        row("spark", 8.0, 2), row("ray", 8.0, 2)]))
    rows = [ln for ln in md.splitlines() if ln.startswith("| 2 ")]
    assert len(rows) == 2
    assert "| 1 |" in rows[0] and "2.00×" in rows[0]
    assert "| 2 |" in rows[1] and "1.00×" in rows[1]


def test_resolve_partitions_zero_means_one_per_group():
    assert resolve_partitions(0, n_groups=10_000) == 10_000
    assert resolve_partitions(256, n_groups=10_000) == 256
