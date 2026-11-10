"""Synthetic SERP L2 candidate rows shared by `ml/tests` and `ml/tests_torch`."""

import random

import pandas as pd


def rows(**overrides):
    """One candidate row as the endpoint sees it: looked-up session/user/item features plus the request fields."""
    row = {"session_id": "s1", "user_id": "u1", "product_id": "B01", "is_guest": 0, "l1_rank": 1, "l1_score": 0.8,
           "price": 400.0, "brand": "canon", "category": "Camera & Photo",
           "sess_n_searches_30m": 1, "sess_min_price_30m": 300.0, "sess_max_price_30m": 500.0,
           "sess_std_price_30m": 50.0, "sess_last_price": 450.0,
           "sess_n_clicks_30m": 3, "sess_n_carts_30m": 1, "sess_cat_cnt_30m": 2, "sess_avg_price_30m": 400.0,
           "sess_last_category": "Camera & Photo", "sess_last_brand": "canon", "sess_last_event_type": "click",
           "user_n_clicks_30d": 8, "user_n_carts_30d": 2, "user_cat_cnt_30d": 5, "user_median_price_30d": 100.0,
           "item_clicks_7d": 12, "item_carts_7d": 3}
    return pd.DataFrame([row | overrides])


NO_HISTORY = {c: None for c in ("sess_n_searches_30m", "sess_min_price_30m", "sess_max_price_30m",
                                "sess_std_price_30m", "sess_last_price", "sess_n_clicks_30m", "sess_n_carts_30m",
                                "sess_cat_cnt_30m", "sess_avg_price_30m", "sess_last_category", "sess_last_brand",
                                "sess_last_event_type", "user_n_clicks_30d", "user_n_carts_30d", "user_cat_cnt_30d",
                                "user_median_price_30d", "item_clicks_7d", "item_carts_7d")}


CATEGORY = "Camera & Photo"
BRANDS = ["canon", "sony", "nikon", "fuji", "olympus", "panasonic"]


def searches(n, seed, size=12):
    """Searches of `size` candidates in L1 order; the engaged one is in the session's last brand, carted (2) when
    it is also near the session's price."""
    rng, out = random.Random(seed), []
    for g in range(n):
        last = rng.choice(BRANDS)
        hit = rng.randrange(size)
        for rank in range(1, size + 1):
            brand = last if rank - 1 == hit else rng.choice([b for b in BRANDS if b != last])
            price = rng.choice([100.0, 400.0])
            label = 0 if brand != last else (2 if price == 400.0 else 1)
            out.append(rows(group_id=f"g{g}", l1_rank=rank, l1_score=1 - rank / 100, brand=brand, price=price,
                            category=CATEGORY, sess_last_brand=last, label=label))
    return pd.concat(out, ignore_index=True)
