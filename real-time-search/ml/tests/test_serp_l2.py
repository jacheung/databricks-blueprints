import math

import numpy as np
import pandas as pd
import pytest

from rts_ml.qac_l2 import fit_ranker, slice_report
from rts_ml.serp_l2 import MODEL_COLUMNS, add_crosses, model_matrix, ndcg_per_group, pack_group
from serp_l2_data import NO_HISTORY, rows, searches


def test_crosses_match_the_candidate_to_the_session_and_user():
    out = add_crosses(pd.concat([rows(), rows(brand="sony", category="Electronics", price=100.0)])).reset_index()
    assert list(out["brand_is_last"]) == [1, 0] and list(out["cat_is_last"]) == [1, 0]
    assert out["sess_cat_share"][0] == 2 / 4 and out["user_cat_share"][0] == 5 / 10
    # The candidate's own price against the session's average, its last engaged price, and the user's median.
    assert math.isclose(out["price_gap_session"][1], math.log(4))
    assert math.isclose(out["price_gap_last"][1], math.log(4.5))
    assert math.isclose(out["price_gap_user"][1], 0.0, abs_tol=1e-12)


def test_the_model_sees_numbers_only_with_no_nulls_and_no_identities():
    X = model_matrix(pd.concat([rows(), rows(**NO_HISTORY, brand=None, is_guest=1)]))
    assert list(X.columns) == list(MODEL_COLUMNS)
    assert X.dtypes.eq(np.float32).all() and not X.isna().any().any()
    assert not {"category", "brand", "sess_last_category", "sess_last_brand", "session_id", "user_id",
                "product_id"} & set(X)


def test_missing_history_is_flagged_not_confused_with_zero():
    X = model_matrix(pd.concat([rows(), rows(**NO_HISTORY)])).reset_index(drop=True)
    assert list(X["has_session"]) == [1, 0] and list(X["has_user"]) == [1, 0] and list(X["has_item"]) == [1, 0]
    assert list(X["price_gap_session_missing"]) == [0, 1]
    assert X.loc[1, "price_gap_session"] == 0


def test_last_event_type_is_one_hot():
    X = model_matrix(pd.concat([rows(sess_last_event_type="add_to_cart"), rows(sess_last_event_type=None)]))
    assert X[["last_search", "last_click", "last_add_to_cart"]].values.tolist() == [[0, 0, 1], [0, 0, 0]]


def test_pack_group_orders_by_l1_rank_and_pads():
    g = pd.DataFrame({"l1_rank": [2, 1, 3], "label": [0, 2, 1], "f": [20.0, 10.0, 30.0]})
    x, y, mask = pack_group(g, ["f"], n=5)
    assert x.shape == (5, 1) and x[:3, 0].tolist() == [10.0, 20.0, 30.0] and x[3:].sum() == 0
    assert y.tolist() == [2, 0, 1, 0, 0] and mask.tolist() == [1, 1, 1, 0, 0]
    with pytest.raises(ValueError):
        pack_group(g, ["f"], n=2)


def test_ndcg_at_10_per_group_uses_graded_gains():
    df = pd.DataFrame({"group_id": ["a"] * 3 + ["b"] * 2, "label": [0, 2, 1, 1, 0], "s": [3, 2, 1, 2, 1]})
    m = ndcg_per_group(df, "s").set_index("group_id")["ndcg"]
    dcg = 3 / math.log2(3) + 1 / math.log2(4)
    idcg = 3 / math.log2(2) + 1 / math.log2(3)
    assert math.isclose(m["a"], dcg / idcg) and m["b"] == 1.0


def test_ndcg_skips_groups_with_no_engagement():
    df = pd.DataFrame({"group_id": ["a", "a", "b"], "label": [0, 0, 1], "s": [1, 2, 1]})
    assert ndcg_per_group(df, "s")["group_id"].tolist() == ["b"]


def test_lightgbm_comparator_reads_the_same_matrix():
    ranker = fit_ranker(searches(200, seed=0), searches(50, seed=1), matrix=model_matrix, eval_at=10)
    test = searches(100, seed=2)
    test["l1"], test["l2"] = -test["l1_rank"], ranker.predict(model_matrix(test))
    assert ndcg_per_group(test, "l2")["ndcg"].mean() > ndcg_per_group(test, "l1")["ndcg"].mean() + 0.3


def test_slice_report_takes_ndcg():
    df = pd.DataFrame({"group_id": ["a", "a", "b", "b"], "label": [0, 1, 1, 0], "l1": [2, 1, 2, 1],
                       "l2": [1, 2, 2, 1], "is_guest": [1, 1, 0, 0]})
    report = slice_report(df, ["is_guest"], per_group=ndcg_per_group, metrics=("ndcg",), seed=0)
    overall = next(r for r in report if r["slice"] == "all")
    assert overall["metric"] == "ndcg" and overall["l2"] == 1.0 and overall["lift"] > 0
