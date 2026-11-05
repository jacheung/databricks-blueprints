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


# Personas: the users the demo signs in as, decoupled from the replayed clickstream. Each configured persona
# (`app.demo_personas`: name + category) gets the simulated user most concentrated in that category in the user
# features' latest values. User history drives the L2 lift through category share (step 0, 2026-11-05: median price
# barely moved the rankings), so personas differ by category.
PERSONA_COLUMNS = ("persona_id", "name", "user_id", "category", "category_share", "category_events_30d",
                   "n_clicks_30d", "median_price_30d", "features_as_of")


def pick_personas(wanted: list[dict], category_rows: list[tuple], users: dict[str, dict], min_events: int) -> list[dict]:
    """`category_rows`: (user_id, category, user_cat_cnt_30d); `users`: user_id -> user_n_clicks_30d and
    user_median_price_30d. Per persona: among users with `min_events`+ events in the persona's category, the highest
    share of their history in it, then the most events, then user_id; a user is used once."""
    totals, counts = {}, {}
    for user_id, category, n in category_rows:
        totals[user_id] = totals.get(user_id, 0) + n
        counts[(user_id, category)] = n
    picks, used = [], set()
    for i, p in enumerate(wanted, start=1):
        pool = [(n / totals[u], n, u) for (u, c), n in counts.items()
                if c == p["category"] and n >= min_events and u not in used and u in users]
        if not pool:
            raise ValueError(f"no user with {min_events}+ events of history in {p['category']} for {p['name']}")
        share, n, user_id = min(pool, key=lambda t: (-t[0], -t[1], t[2]))
        used.add(user_id)
        picks.append({"persona_id": f"p{i}", "name": p["name"], "user_id": user_id, "category": p["category"],
                      "category_share": round(share, 3), "category_events_30d": n,
                      "n_clicks_30d": users[user_id]["user_n_clicks_30d"],
                      "median_price_30d": users[user_id]["user_median_price_30d"]})
    return picks
