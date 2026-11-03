"""Contract: the session event schema, owned by data, produced by the app, read by ML for training.

One schema for `session_events` (clickstream_sim), the Kafka payload (kafka_stream), and live events from the app.
"""

from __future__ import annotations

import json
from datetime import timezone

EVENT_FIELDS = [("session_id", "string"), ("user_id", "string"), ("search_id", "string"), ("event_id", "string"),
                ("event_time", "timestamp"), ("event_type", "string"), ("query_id", "bigint"), ("query", "string"),
                ("product_id", "string"), ("category", "string"), ("brand", "string"), ("price", "double"),
                ("device", "string")]
REQUIRED_FIELDS = ("session_id", "event_id", "event_time", "event_type")
_FIELDS = [name for name, _ in EVENT_FIELDS]
_JSON_TYPE = {"string": {"type": "string"}, "bigint": {"type": "integer"}, "double": {"type": "number"},
              "timestamp": {"type": "string", "format": "date-time"}}


def payload_json_schema() -> str:
    """JSON Schema for the Kafka event payload; event_time is an ISO-8601 string so features see a TIMESTAMP."""
    return json.dumps({"type": "object", "properties": {n: _JSON_TYPE[t] for n, t in EVENT_FIELDS}})


def event_payload(event: dict) -> tuple[bytes, bytes]:
    """(key, value) for one event: keyed by session_id, value is the shared event schema as JSON."""
    unknown = set(event) - set(_FIELDS)
    if unknown:
        raise ValueError(f"unknown event fields: {sorted(unknown)}")
    missing = [f for f in REQUIRED_FIELDS if event.get(f) is None]
    if missing:
        raise ValueError(f"missing required event fields: {missing}")
    body = {name: event.get(name) for name in _FIELDS}
    t = body["event_time"].astimezone(timezone.utc)
    body["event_time"] = t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"
    return body["session_id"].encode(), json.dumps(body).encode()
