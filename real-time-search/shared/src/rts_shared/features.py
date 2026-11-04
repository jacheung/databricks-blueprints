"""Contract: the clickstream features ML may look up, by name, with their lookup keys. Owned by data.

Names only, no definitions (those live in `rts_data.feature_specs`). The data tests assert the specs match this
exactly, so renaming or re-keying a feature fails there before it breaks an ML training set or endpoint.
Keys are the request-side column names: session features read a Kafka Stream (`value.` fields) but are looked up
by `session_id`.
"""

from __future__ import annotations


def feature_keys(cfg: dict) -> dict[str, tuple[str, ...]]:
    """{feature name: lookup keys}. Window suffixes follow `feature_store` in the config."""
    fs = cfg["feature_store"]
    s, u, i = f"{fs['session_window_minutes']}m", f"{fs['user_window_days']}d", f"{fs['item_window_days']}d"
    session, user, item = ("session_id",), ("user_id",), ("product_id",)
    return {
        f"sess_n_clicks_{s}": session, f"sess_n_carts_{s}": session, f"sess_n_searches_{s}": session,
        f"sess_avg_price_{s}": session, f"sess_min_price_{s}": session, f"sess_max_price_{s}": session,
        f"sess_std_price_{s}": session, f"sess_cat_cnt_{s}": (*session, "category"),
        "sess_last_category": session, "sess_last_brand": session, "sess_last_price": session,
        "sess_last_event_type": session,
        f"user_median_price_{u}": user, f"user_n_clicks_{u}": user, f"user_n_carts_{u}": user,
        f"user_cat_cnt_{u}": (*user, "category"),
        f"item_clicks_{i}": item, f"item_carts_{i}": item,
    }
