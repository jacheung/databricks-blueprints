"""QAC L2: session-aware LightGBM reranker over the L1 completions.

Every input is a Feature View feature (session from the Kafka stream, user from Delta, candidate fields from the
request), so training and serving read the same values. The crosses that match a candidate to the session are
computed here, inside the logged model, so they can't drift between the two.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from rts_shared.retrieval import QAC_L2_REQUEST_COLUMNS

# Session and user features as the Feature Views name them (`rts_shared.features`, 30-minute / 30-day windows).
SESSION_NUMERIC = ("sess_n_clicks_30m", "sess_n_carts_30m", "sess_n_searches_30m", "sess_avg_price_30m",
                   "sess_min_price_30m", "sess_max_price_30m", "sess_std_price_30m", "sess_last_price")
USER_NUMERIC = ("user_median_price_30d", "user_n_clicks_30d", "user_n_carts_30d")
CROSSES = ("sess_cat_share", "cat_is_last", "brand_is_last", "user_cat_share", "price_gap_session", "price_gap_user")
IDENTITIES = ("category", "brand")  # matched against the session through the crosses, never scored directly
EVENT_TYPE_CODES = {"search": 0, "click": 1, "add_to_cart": 2}
LGBM_PARAMS = {"objective": "lambdarank", "num_leaves": 31, "learning_rate": 0.05, "n_estimators": 1000,
               "min_child_samples": 20, "verbose": -1}
EARLY_STOPPING_ROUNDS = 50
MODEL_COLUMNS = (*SESSION_NUMERIC, "sess_last_event_type", *USER_NUMERIC,
                 *(c for c in QAC_L2_REQUEST_COLUMNS if c not in IDENTITIES), *CROSSES)


def _share(part: pd.Series, *totals: pd.Series) -> pd.Series:
    total = sum(t.fillna(0) for t in totals)
    return (part.fillna(0) / total.where(total > 0)).fillna(0.0)


def _log_gap(price: pd.Series, reference: pd.Series) -> pd.Series:
    """How far the candidate's typical clicked price sits from a reference price tier; NaN if either is missing."""
    return np.abs(np.log(price.astype(float) / reference.astype(float)))


def add_crosses(df: pd.DataFrame) -> pd.DataFrame:
    """The candidate x session/user features LightGBM scores on; returns a copy with the cross columns added."""
    out = df.copy()
    out["sess_cat_share"] = _share(df["sess_cat_cnt_30m"], df["sess_n_clicks_30m"], df["sess_n_carts_30m"])
    out["user_cat_share"] = _share(df["user_cat_cnt_30d"], df["user_n_clicks_30d"], df["user_n_carts_30d"])
    # Nulls never match: a session with no engaged event, or a completion with no clicked brand.
    out["cat_is_last"] = (df["category"].notna() & (df["category"] == df["sess_last_category"])).astype(int)
    out["brand_is_last"] = (df["brand"].notna() & (df["brand"] == df["sess_last_brand"])).astype(int)
    out["price_gap_session"] = _log_gap(df["median_clicked_price"], df["sess_avg_price_30m"])
    out["price_gap_user"] = _log_gap(df["median_clicked_price"], df["user_median_price_30d"])
    return out


def model_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Raw candidate rows (features + request fields) -> the float matrix LightGBM scores, in MODEL_COLUMNS order."""
    out = add_crosses(df)
    out["sess_last_event_type"] = out["sess_last_event_type"].map(EVENT_TYPE_CODES)
    return out[list(MODEL_COLUMNS)].astype(float)


def rank_metrics(df: pd.DataFrame, score: str, group: str = "group_id", label: str = "label",
                 k: int = 5) -> pd.DataFrame:
    """Per group: reciprocal rank of the positive within the top k (`mrr`), and whether it ranks first (`success1`).
    Groups whose candidates hold no positive score 0, so L1 misses count against both L1 and L2."""
    ranks = df.groupby(group)[score].rank(ascending=False, method="first")
    hit = df[label] == 1
    rr = (1.0 / ranks).where(hit & (ranks <= k), 0.0)
    out = pd.DataFrame({group: df[group], "mrr": rr, "success1": (hit & (ranks == 1)).astype(float)})
    return out.groupby(group, as_index=False).max()


def _grouped(df: pd.DataFrame, group: str) -> tuple[pd.DataFrame, list[int]]:
    """LightGBM reads each query group as a contiguous block of rows."""
    df = df.sort_values(group, kind="stable")
    return df, df.groupby(group, sort=False).size().tolist()


def fit_ranker(train: pd.DataFrame, valid: pd.DataFrame, group: str = "group_id", label: str = "label"):
    """LambdaRank on raw candidate rows; early-stops on validation NDCG@5."""
    import lightgbm as lgb

    train, train_groups = _grouped(train, group)
    valid, valid_groups = _grouped(valid, group)
    ranker = lgb.LGBMRanker(**LGBM_PARAMS)
    ranker.fit(model_matrix(train), train[label], group=train_groups,
               eval_set=[(model_matrix(valid), valid[label])], eval_group=[valid_groups], eval_at=[5],
               callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)])
    return ranker


def score(ranker, df: pd.DataFrame) -> np.ndarray:
    """L2 scores for raw candidate rows, in row order; higher ranks first."""
    return ranker.predict(model_matrix(df))


def bootstrap_ci(values, n_boot: int = 1000, seed: int = 0, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean. Pass per-group L2 - L1 differences for a paired CI on the lift."""
    values = np.asarray(values, dtype=float)
    means = np.random.default_rng(seed).choice(values, size=(n_boot, len(values))).mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def slice_report(df: pd.DataFrame, slices: list[str], group: str = "group_id", label: str = "label",
                 n_boot: int = 1000, seed: int = 0) -> list[dict]:
    """L1 vs L2 on scored candidate rows (`l1`, `l2` score columns), overall and per value of each slice column
    (constant within a group). One row per (slice, value, metric) with a paired bootstrap CI on the lift."""
    per_group = (rank_metrics(df, "l1", group, label)
                 .merge(rank_metrics(df, "l2", group, label), on=group, suffixes=("_l1", "_l2"))
                 .merge(df.groupby(group, as_index=False)[slices].first(), on=group))
    parts = [("all", "all", per_group)] + [(s, v, g) for s in slices for v, g in per_group.groupby(s)]
    report = []
    for name, value, g in parts:
        for metric in ("mrr", "success1"):
            l1, l2 = g[f"{metric}_l1"], g[f"{metric}_l2"]
            lo, hi = bootstrap_ci(l2 - l1, n_boot=n_boot, seed=seed)
            report.append({"slice": name, "value": value, "metric": metric, "n": len(g), "l1": l1.mean(),
                           "l2": l2.mean(), "lift": l2.mean() - l1.mean(), "lift_lo": lo, "lift_hi": hi})
    return report
