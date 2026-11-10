import json
from datetime import datetime, timezone

import pytest

from rts_shared.events import EVENT_FIELDS, event_payload, payload_json_schema


def test_payload_schema_matches_event_columns():
    schema = json.loads(payload_json_schema())
    assert list(schema["properties"]) == [name for name, _ in EVENT_FIELDS]
    assert schema["properties"]["event_time"] == {"type": "string", "format": "date-time"}
    assert schema["properties"]["price"] == {"type": "number"}
    assert schema["properties"]["query_id"] == {"type": "integer"}
    assert {"session_id", "event_id", "event_type", "category"} <= set(schema["properties"])


def test_event_payload_is_the_shared_schema_as_json():
    event = {"session_id": "s1", "user_id": None, "search_id": "s1-0", "event_id": "s1-001",
             "event_time": datetime(2026, 10, 29, 22, 0, 25, 123456, tzinfo=timezone.utc), "event_type": "click",
             "query_id": 7, "query": "canon lens", "product_id": "B01", "category": "Camera & Photo",
             "brand": "canon", "price": 2000.0, "device": "desktop"}
    key, value = event_payload(event)
    assert key == b"s1"  # keyed by session: one session's events stay ordered in one partition
    body = json.loads(value)
    assert body["event_time"] == "2026-10-29T22:00:25.123Z"
    assert body["user_id"] is None and body["price"] == 2000.0


def test_event_payload_rejects_unknown_or_missing_fields():
    with pytest.raises(ValueError, match="unknown"):
        event_payload({"session_id": "s1", "event_id": "e", "event_time": datetime.now(timezone.utc),
                       "event_type": "click", "colour": "red"})
    with pytest.raises(ValueError, match="event_id"):
        event_payload({"session_id": "s1", "event_time": datetime.now(timezone.utc), "event_type": "click"})
