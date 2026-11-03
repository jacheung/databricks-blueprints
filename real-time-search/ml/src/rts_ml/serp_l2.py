"""SERP L2: the model inputs and metrics for the session-aware reranker over the L1 hybrid top 200.

Pandas only (no torch), so the CPU task that builds the training set imports it too. Every input is a Feature View
feature (session from the Kafka stream, user and item from Delta, candidate fields from the request); the crosses
that match a candidate to the session are computed here, inside the logged model, so training and serving can't
drift. Brand and category identities never reach the model, only whether they match.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from rts_ml.qac_l2 import _log_gap, _share

# Feature View names (`rts_shared.features`, 30-minute / 30-day / 7-day windows).
COUNTS = ("sess_n_clicks_30m", "sess_n_carts_30m", "sess_n_searches_30m", "user_n_clicks_30d", "user_n_carts_30d",
          "item_clicks_7d", "item_carts_7d")
PRICES = ("price", "sess_avg_price_30m", "sess_min_price_30m", "sess_max_price_30m", "sess_last_price",
          "user_median_price_30d")
LAST_EVENT = {"search": "last_search", "click": "last_click", "add_to_cart": "last_add_to_cart"}
GAPS = {"price_gap_session": "sess_avg_price_30m", "price_gap_user": "user_median_price_30d",
        "price_gap_last": "sess_last_price"}
MODEL_COLUMNS = (*COUNTS, *(f"log_{c}" for c in PRICES), "sess_std_price_30m", *LAST_EVENT.values(),
                 "has_session", "has_user", "has_item", "is_guest", "l1_log_rank", "l1_score",
                 "sess_cat_share", "user_cat_share", "cat_is_last", "brand_is_last",
                 *GAPS, *(f"{g}_missing" for g in GAPS))


def add_crosses(df: pd.DataFrame) -> pd.DataFrame:
    """The candidate x session/user features; returns a copy with the cross columns added (gaps NaN if missing)."""
    out = df.copy()
    out["sess_cat_share"] = _share(df["sess_cat_cnt_30m"], df["sess_n_clicks_30m"], df["sess_n_carts_30m"])
    out["user_cat_share"] = _share(df["user_cat_cnt_30d"], df["user_n_clicks_30d"], df["user_n_carts_30d"])
    # Nulls never match: a session with no engaged event, or a product with no brand.
    out["cat_is_last"] = (df["category"].notna() & (df["category"] == df["sess_last_category"])).astype(int)
    out["brand_is_last"] = (df["brand"].notna() & (df["brand"] == df["sess_last_brand"])).astype(int)
    for gap, reference in GAPS.items():
        out[gap] = _log_gap(df["price"], df[reference])
    return out


def model_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Raw candidate rows -> float32 matrix in MODEL_COLUMNS order, no NaN: a neural net can't skip missing values,
    so missing history reads as 0 plus a flag."""
    x = add_crosses(df)
    out = pd.DataFrame(index=df.index)
    for c in COUNTS:
        out[c] = np.log1p(x[c].astype(float).fillna(0))
    for c in PRICES:
        out[f"log_{c}"] = np.log(x[c].astype(float).clip(lower=0.01)).fillna(0)
    out["sess_std_price_30m"] = np.log1p(x["sess_std_price_30m"].astype(float).fillna(0))
    for event, column in LAST_EVENT.items():
        out[column] = (x["sess_last_event_type"] == event).astype(float)
    out["has_session"] = x["sess_last_event_type"].notna().astype(float)
    out["has_user"] = x["user_n_clicks_30d"].notna().astype(float)
    out["has_item"] = (x["item_clicks_7d"].notna() | x["item_carts_7d"].notna()).astype(float)
    out["is_guest"] = x["is_guest"].astype(float)
    out["l1_log_rank"] = np.log(x["l1_rank"].astype(float))
    out["l1_score"] = x["l1_score"].astype(float).fillna(0)
    for c in ("sess_cat_share", "user_cat_share", "cat_is_last", "brand_is_last"):
        out[c] = x[c].astype(float)
    for gap in GAPS:
        out[f"{gap}_missing"] = x[gap].isna().astype(float)
        out[gap] = x[gap].fillna(0)
    return out[list(MODEL_COLUMNS)].astype(np.float32)


def pack_group(g: pd.DataFrame, columns: list[str], n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One search's candidate rows -> fixed-size (x [n, F], label [n], mask [n]) in L1 order, zero-padded."""
    if len(g) > n:
        raise ValueError(f"{len(g)} candidates in one search, more than {n}")
    g = g.sort_values("l1_rank", kind="stable")
    x, y, mask = np.zeros((n, len(columns)), np.float32), np.zeros(n, np.float32), np.zeros(n, np.float32)
    x[:len(g)], y[:len(g)], mask[:len(g)] = g[columns].to_numpy(np.float32), g["label"].to_numpy(np.float32), 1
    return x, y, mask


def ndcg_per_group(df: pd.DataFrame, score: str, group: str = "group_id", label: str = "label",
                   k: int = 10) -> pd.DataFrame:
    """NDCG@k per group, gain 2^label - 1 (cart 2 > click 1). Groups with no engaged candidate have no ideal order
    and are left out, for every scorer alike."""
    rank = df.groupby(group)[score].rank(ascending=False, method="first")
    ideal = df.groupby(group)[label].rank(ascending=False, method="first")
    gain = np.power(2.0, df[label].astype(float)) - 1
    parts = pd.DataFrame({group: df[group],
                          "dcg": (gain / np.log2(rank + 1)).where(rank <= k, 0.0),
                          "idcg": (gain / np.log2(ideal + 1)).where(ideal <= k, 0.0)})
    sums = parts.groupby(group, as_index=False)[["dcg", "idcg"]].sum()
    sums = sums[sums["idcg"] > 0]
    return pd.DataFrame({group: sums[group], "ndcg": sums["dcg"] / sums["idcg"]}).reset_index(drop=True)
