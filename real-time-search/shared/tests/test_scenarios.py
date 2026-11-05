from datetime import datetime, timedelta, timezone

import pytest

from rts_shared.events import EVENT_FIELDS, event_payload
from rts_shared.scenarios import MAX_GAP_MS, pick_personas, replay

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)

EVENTS = [
    {"offset_ms": 0, "search_id": "s9-0", "event_type": "search", "query_id": 7, "query": "canon lens",
     "product_id": None, "category": "Camera & Photo", "brand": None, "price": None, "device": "desktop"},
    {"offset_ms": 30_000, "search_id": "s9-0", "event_type": "click", "query_id": 7, "query": "canon lens",
     "product_id": "B01", "category": "Camera & Photo", "brand": "canon", "price": 450.0, "device": "desktop"},
    {"offset_ms": 31_000, "search_id": "s9-1", "event_type": "search", "query_id": 8, "query": "canon 50mm",
     "product_id": None, "category": "Camera & Photo", "brand": None, "price": None, "device": "desktop"},
]


def test_replay_runs_under_a_fresh_session_and_ends_now():
    out = replay(EVENTS, session_id="demo-1", user_id="u42", now=NOW)
    assert [e["session_id"] for e in out] == ["demo-1"] * 3
    assert [e["user_id"] for e in out] == ["u42"] * 3
    assert [e["event_id"] for e in out] == ["demo-1-000", "demo-1-001", "demo-1-002"]
    assert out[-1]["event_time"] == NOW


def test_replay_keeps_order_and_caps_gaps():
    times = [e["event_time"] for e in replay(EVENTS, "demo-1", None, NOW)]
    # 30 s gap capped to MAX_GAP_MS; the 1 s gap kept.
    assert times[1] - times[0] == timedelta(milliseconds=MAX_GAP_MS)
    assert times[2] - times[1] == timedelta(seconds=1)


def test_replay_remaps_search_ids_so_clicks_stay_with_their_search():
    out = replay(EVENTS, "demo-1", None, NOW)
    assert [e["search_id"] for e in out] == ["demo-1-0", "demo-1-0", "demo-1-1"]


def test_replayed_events_are_valid_payloads():
    for e in replay(EVENTS, "demo-1", "u42", NOW):
        assert set(e) == {name for name, _ in EVENT_FIELDS}
        event_payload(e)  # raises on unknown or missing fields


# Personas: who the demo can sign in as. Rows are the user features' latest values.
CAT_ROWS = [  # (user_id, category, user_cat_cnt_30d)
    ("u1", "Electronics", 30), ("u1", "Books & Media", 2),   # 94% Electronics, 32 events
    ("u2", "Electronics", 20),                               # 100%, but fewer events than u3
    ("u3", "Electronics", 40),                               # 100%, 40 events: the pick
    ("u4", "Camera & Photo", 10),                            # below min_events
    ("u5", "Camera & Photo", 16), ("u5", "Baby", 4),         # 80%, 20 events: the pick
]
USERS = {u: {"user_n_clicks_30d": 9, "user_median_price_30d": 50.0} for u in ("u1", "u2", "u3", "u4", "u5")}
WANTED = [{"name": "Ali Ghodsi", "category": "Electronics"}, {"name": "Reynold Xin", "category": "Camera & Photo"}]


def test_pick_personas_takes_the_most_concentrated_user_per_category():
    picks = pick_personas(WANTED, CAT_ROWS, USERS, min_events=15)
    assert [(p["name"], p["user_id"]) for p in picks] == [("Ali Ghodsi", "u3"), ("Reynold Xin", "u5")]
    assert picks[1]["category_share"] == 0.8 and picks[1]["category_events_30d"] == 16
    assert picks[1]["n_clicks_30d"] == 9 and picks[1]["median_price_30d"] == 50.0
    assert [p["persona_id"] for p in picks] == ["p1", "p2"]


def test_pick_personas_never_reuses_a_user_and_fails_when_a_category_has_no_one():
    twice = [WANTED[0], {"name": "Matei Zaharia", "category": "Electronics"}]
    assert [p["user_id"] for p in pick_personas(twice, CAT_ROWS, USERS, min_events=15)] == ["u3", "u2"]
    with pytest.raises(ValueError, match="Baby"):
        pick_personas([{"name": "X", "category": "Baby"}], CAT_ROWS, USERS, min_events=15)
