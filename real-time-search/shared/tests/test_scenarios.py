from datetime import datetime, timedelta, timezone

from rts_shared.events import EVENT_FIELDS, event_payload
from rts_shared.scenarios import MAX_GAP_MS, replay

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
