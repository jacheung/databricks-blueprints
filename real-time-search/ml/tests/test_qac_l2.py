import math
import random

import pandas as pd

from rts_ml.qac_l2 import (MODEL_COLUMNS, add_crosses, bootstrap_ci, fit_ranker, model_matrix, rank_metrics, score,
                           slice_report)


def rows(**overrides):
    """One candidate row as the endpoint sees it: looked-up features plus the request fields."""
    row = {"session_id": "s1", "user_id": "u1", "is_guest": 0, "category": "Camera & Photo", "brand": "canon",
           "median_clicked_price": 400.0, "l1_rank": 1, "prefix_len": 2, "n_tokens": 2, "is_word_boundary": False,
           "pop_30d": 50, "pop_7d": 12, "ctr": 0.6, "category_share": 0.9, "brand_share": 0.4,
           "sess_n_searches_30m": 1, "sess_min_price_30m": 300.0, "sess_max_price_30m": 500.0,
           "sess_std_price_30m": 50.0, "sess_last_price": 450.0,
           "sess_n_clicks_30m": 3, "sess_n_carts_30m": 1, "sess_cat_cnt_30m": 2, "sess_avg_price_30m": 400.0,
           "sess_last_category": "Camera & Photo", "sess_last_brand": "canon", "sess_last_event_type": "click",
           "user_n_clicks_30d": 8, "user_n_carts_30d": 2, "user_cat_cnt_30d": 5, "user_median_price_30d": 100.0}
    return pd.DataFrame([row | overrides])


def test_category_shares_are_the_candidate_category_share_of_engaged_events():
    out = add_crosses(rows()).iloc[0]
    assert out["sess_cat_share"] == 2 / 4
    assert out["user_cat_share"] == 5 / 10


def test_category_shares_are_zero_without_engaged_events():
    # Count features come back null when the window holds no matching events.
    out = add_crosses(rows(sess_n_clicks_30m=None, sess_n_carts_30m=None, sess_cat_cnt_30m=None,
                           user_n_clicks_30d=None, user_n_carts_30d=None, user_cat_cnt_30d=None)).iloc[0]
    assert out["sess_cat_share"] == 0 and out["user_cat_share"] == 0


def test_matches_the_sessions_last_category_and_brand():
    out = add_crosses(pd.concat([rows(), rows(category="Electronics", brand="sony")])).reset_index()
    assert list(out["cat_is_last"]) == [1, 0] and list(out["brand_is_last"]) == [1, 0]


def test_no_match_without_a_last_value_or_a_candidate_brand():
    out = add_crosses(pd.concat([rows(sess_last_category=None, sess_last_brand=None),
                                 rows(brand=None, sess_last_brand=None)])).reset_index()
    assert list(out["cat_is_last"]) == [0, 1] and list(out["brand_is_last"]) == [0, 0]


def test_price_gaps_are_log_distance_from_the_session_and_user_price():
    out = add_crosses(rows(median_clicked_price=200.0, sess_avg_price_30m=400.0, user_median_price_30d=100.0)).iloc[0]
    assert math.isclose(out["price_gap_session"], math.log(2)) and math.isclose(out["price_gap_user"], math.log(2))


def test_price_gaps_are_missing_when_either_price_is():
    out = add_crosses(pd.concat([rows(median_clicked_price=None),
                                 rows(sess_avg_price_30m=None, user_median_price_30d=None)])).reset_index()
    assert out[["price_gap_session", "price_gap_user"]].isna().all().all()


def test_the_model_sees_numbers_only_never_brand_or_category_identities():
    X = model_matrix(rows())
    assert list(X.columns) == list(MODEL_COLUMNS)
    assert all(pd.api.types.is_numeric_dtype(t) for t in X.dtypes)
    assert not {"category", "brand", "sess_last_category", "sess_last_brand", "session_id", "user_id"} & set(X)


def test_last_event_type_is_encoded_and_missing_stays_missing():
    X = model_matrix(pd.concat([rows(sess_last_event_type="add_to_cart"), rows(sess_last_event_type=None)]))
    assert X["sess_last_event_type"].iloc[0] == 2 and math.isnan(X["sess_last_event_type"].iloc[1])


def test_rank_metrics_per_group_mrr_at_5_and_success_at_1():
    df = pd.DataFrame({"group_id": ["a"] * 3 + ["b"] * 3 + ["c"] * 2 + ["d"] * 6,
                       "label": [0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
                       "score": [3, 2, 1, 9, 8, 7, 2, 1, 6, 5, 4, 3, 2, 1]})
    m = rank_metrics(df, "score").set_index("group_id")
    assert m.loc["a"].tolist() == [0.5, 0.0]   # positive at rank 2
    assert m.loc["b"].tolist() == [1.0, 1.0]
    assert m.loc["c"].tolist() == [0.0, 0.0]   # no positive among the candidates: an L1 miss counts as 0
    assert m.loc["d"].tolist() == [0.0, 0.0]   # positive at rank 6, outside the top 5


CATEGORIES = ["Camera & Photo", "Electronics", "Pet Supplies", "Baby", "Automotive"]


def searches(n, seed):
    """Groups of 8 candidates in L1 order; the submitted one is the candidate in the session's last category."""
    rng, out = random.Random(seed), []
    for g in range(n):
        last = rng.choice(CATEGORIES)
        cats = rng.sample(CATEGORIES, len(CATEGORIES)) + [rng.choice(CATEGORIES[1:]) for _ in range(3)]
        cats = [c for c in cats if c != last][:7]
        cats.insert(rng.randrange(8), last)
        for rank, cat in enumerate(cats, start=1):
            out.append(rows(group_id=f"g{g}", l1_rank=rank, category=cat, sess_last_category=last,
                            pop_30d=100 - rank, label=int(cat == last)))
    return pd.concat(out, ignore_index=True)


def test_ranker_learns_session_intent_and_beats_the_l1_order():
    train = searches(300, seed=0).sample(frac=1, random_state=0)  # grouping must not rely on row order
    valid, test = searches(100, seed=1), searches(200, seed=2)
    ranker = fit_ranker(train, valid)
    test["l1"], test["l2"] = -test["l1_rank"], score(ranker, test)
    assert rank_metrics(test, "l2")["mrr"].mean() > rank_metrics(test, "l1")["mrr"].mean() + 0.3


def test_saved_model_scores_raw_rows_like_training(tmp_path):
    import mlflow

    from rts_ml.qac_l2_model import QacRanker

    ranker = fit_ranker(searches(100, seed=0), searches(50, seed=1))
    # The endpoint strips the lookup keys before calling the model: it gets features and request fields only.
    test = searches(20, seed=2).drop(columns=["session_id", "user_id"])
    mlflow.pyfunc.save_model(tmp_path / "model", python_model=QacRanker(ranker),
                             code_paths=["ml/src/rts_ml", "shared/src/rts_shared"])
    loaded = mlflow.pyfunc.load_model(str(tmp_path / "model"))
    assert list(loaded.predict(test)) == list(score(ranker, test))


def test_bootstrap_ci_brackets_the_mean_and_is_reproducible():
    values = [0.0, 1.0] * 50
    lo, hi = bootstrap_ci(values, seed=7)
    assert lo < 0.5 < hi and (lo, hi) == bootstrap_ci(values, seed=7)
    assert bootstrap_ci([0.25] * 10, seed=7) == (0.25, 0.25)


def test_slice_report_compares_l1_and_l2_overall_and_per_slice():
    # Two guest searches L2 fixes, one logged-in search both rank right.
    df = pd.DataFrame({"group_id": ["a", "a", "b", "b", "c", "c"], "label": [0, 1, 0, 1, 1, 0],
                       "l1": [2, 1, 2, 1, 2, 1], "l2": [1, 2, 1, 2, 2, 1],
                       "is_guest": [1, 1, 1, 1, 0, 0]})
    report = {(r["slice"], r["value"], r["metric"]): r for r in slice_report(df, ["is_guest"], seed=0)}
    overall = report[("all", "all", "success1")]
    assert overall["n"] == 3 and overall["l1"] == 1 / 3 and overall["l2"] == 1.0
    assert math.isclose(overall["lift"], 2 / 3)
    guests, members = report[("is_guest", 1, "mrr")], report[("is_guest", 0, "mrr")]
    assert (guests["n"], guests["l1"], guests["l2"], guests["lift_lo"]) == (2, 0.5, 1.0, 0.5)
    assert (members["n"], members["lift"], members["lift_lo"], members["lift_hi"]) == (1, 0.0, 0.0, 0.0)
