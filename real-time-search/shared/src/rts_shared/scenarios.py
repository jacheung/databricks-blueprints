"""Contract: demo scenarios, owned by data, replayed by the app.

A scenario is the start of a simulated test-split session (its events before its last search) plus that last
search's query. The app replays the events to Kafka under a fresh session id, so the features the rankers see are
computed live from what was just produced.
"""

from __future__ import annotations

from datetime import datetime, timedelta

SCENARIO_COLUMNS = ("scenario_id", "label", "user_id", "intent_category", "intent_brand", "intent_price_target",
                    "next_query", "events")
# Fields of each element of the `events` JSON array; offset_ms is from the scenario's first event.
SCENARIO_EVENT_FIELDS = ("offset_ms", "search_id", "event_type", "query_id", "query", "product_id", "category",
                         "brand", "price", "device")
MAX_GAP_MS = 2000  # replayed gaps are capped so a scenario lands in seconds, well inside the session window


def replay(events: list[dict], session_id: str, user_id: str | None, now: datetime) -> list[dict]:
    """Scenario events as session events: fresh session and event ids, gaps capped, the last one at `now`."""
    offsets, t = [], 0
    for prev, e in zip([None, *events], events):
        t += 0 if prev is None else min(e["offset_ms"] - prev["offset_ms"], MAX_GAP_MS)
        offsets.append(t)
    searches = {}
    out = []
    for i, (e, offset) in enumerate(zip(events, offsets)):
        search_id = searches.setdefault(e["search_id"], f"{session_id}-{len(searches)}")
        out.append({"session_id": session_id, "user_id": user_id, "search_id": search_id,
                    "event_id": f"{session_id}-{i:03d}",
                    "event_time": now - timedelta(milliseconds=offsets[-1] - offset),
                    **{f: e[f] for f in ("event_type", "query_id", "query", "product_id", "category", "brand",
                                         "price", "device")}})
    return out
