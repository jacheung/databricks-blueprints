from datetime import datetime

import pytest

from rts_data.datagen import TOP_BRANDS, Catalog, intent_lift, intent_lift_problems, simulate

CATS = {"Camera & Photo": 150.0, "Pet Supplies": 20.0}


LABEL_MIX = "E" * 12 + "S" * 4 + "C" + "I" * 2   # roughly ESCI US: mostly Exact
BRANDS = ["sony", "canon", "nikon", "acme"]


def tiny_catalog():
    queries, judged = [], {}
    for c, (cat, median) in enumerate(CATS.items()):
        other = next(o for o in CATS if o != cat)
        for q in range(5):
            qid = c * 100 + q
            queries.append((qid, f"{cat.lower()} query {q}", cat))
            # Same mix of labels, brands and prices for every query so only intent drives clicks.
            judged[qid] = [(f"{qid}-{i}", label, other if i % 6 == 5 else cat, BRANDS[i % 4],
                            round(median * (0.4 + i / 10), 2))
                           for i, label in enumerate(LABEL_MIX)]
    return Catalog.build(queries, judged, CATS)


SIM = {"n_sessions": 3000, "n_users": 300, "logged_in_share": 0.6, "intent_session_share": 0.7,
       "mobile_share": 0.6, "seed": 42, "start_date": "2026-08-01", "end_date": "2026-09-15"}


@pytest.fixture(scope="module")
def events():
    return list(simulate(tiny_catalog(), SIM))


def by_session(events):
    sessions = {}
    for e in events:
        sessions.setdefault(e["session_id"], []).append(e)
    return sessions


def test_deterministic_for_a_seed(events):
    again = list(simulate(tiny_catalog(), SIM))
    assert again == events
    other = list(simulate(tiny_catalog(), {**SIM, "seed": 7}))
    assert other != events


def test_event_schema_and_types(events):
    keys = {"session_id", "user_id", "search_id", "event_id", "event_time", "event_type", "query_id", "query", "product_id",
            "category", "brand", "price", "device", "intent_category", "intent_brand", "intent_price_target"}
    assert all(set(e) == keys for e in events)
    assert {e["event_type"] for e in events} == {"search", "click", "back", "add_to_cart"}
    assert all(e["product_id"] is None for e in events if e["event_type"] == "search")
    assert all(e["product_id"] is not None for e in events if e["event_type"] in ("click", "add_to_cart"))


def test_sessions_are_time_ordered_and_start_with_search(events):
    start, end = datetime(2026, 8, 1), datetime(2026, 9, 16)
    for session in by_session(events).values():
        times = [e["event_time"] for e in session]
        assert times == sorted(times) and len(set(times)) == len(times)
        assert session[0]["event_type"] == "search"
        assert start <= times[0] < end


def test_clicks_belong_to_their_search_and_carts_follow_clicks(events):
    for session in by_session(events).values():
        searches, clicked = set(), set()
        for e in session:
            if e["event_type"] == "search":
                searches.add(e["search_id"])
            else:
                assert e["search_id"] in searches
            if e["event_type"] == "click":
                clicked.add(e["product_id"])
            if e["event_type"] == "add_to_cart":
                assert e["product_id"] in clicked


def test_guests_have_no_user_id_and_session_fields_are_constant(events):
    sessions = by_session(events).values()
    assert 0.5 < sum(s[0]["user_id"] is not None for s in sessions) / len(sessions) < 0.7
    for s in sessions:
        assert len({(e["user_id"], e["device"], e["intent_category"], e["intent_brand"],
                     e["intent_price_target"]) for e in s}) == 1


def test_planted_intent_drives_clicks(events):
    # Clicks in intent sessions land in the intent category far more than chance.
    intent_clicks = [e for e in events if e["event_type"] == "click" and e["intent_category"]]
    on_intent = sum(e["category"] == e["intent_category"] for e in intent_clicks) / len(intent_clicks)
    assert on_intent > 0.85
    # And intent sessions search inside their category.
    intent_searches = [e for e in events if e["event_type"] == "search" and e["intent_category"]]
    assert all(e["category"] == e["intent_category"] for e in intent_searches)


def test_exact_matches_are_clicked_most(events):
    cat = tiny_catalog()
    label = {pid: lab for rows in cat.judged.values() for pid, lab, *_ in rows}
    clicks = [label[e["product_id"]] for e in events if e["event_type"] == "click"]
    assert clicks.count("E") > clicks.count("S") > clicks.count("I")


def test_a_realistic_share_of_searches_get_engagement(events):
    searches = {e["search_id"] for e in events if e["event_type"] == "search"}
    engaged = {e["search_id"] for e in events if e["event_type"] in ("click", "add_to_cart")}
    assert 0.4 <= len(engaged) / len(searches) <= 0.6


def test_intent_lifts_brand_and_price_fit_within_a_search(events):
    # Category is fixed by the query; brand and price are what personalization must pick up inside one result list.
    lift = intent_lift(events, tiny_catalog().judged)
    assert lift["brand_match_clicked"] > 1.5 * lift["brand_match_shown"]
    assert lift["price_gap_clicked"] < 0.8 * lift["price_gap_shown"]
    assert lift["searches"] > 500


def test_intent_lift_handles_no_intent_events():
    assert intent_lift([], {})["searches"] == 0


def brand_specific_catalog():
    # Like ESCI: each query's results are mostly one brand, so a brand shopper has to pick the right queries.
    queries, judged = [], {}
    for c, (cat, median) in enumerate(CATS.items()):
        for q in range(40):
            qid = c * 1000 + q
            queries.append((qid, f"{BRANDS[q % 4]} {cat.lower()} {q}", cat))
            judged[qid] = [(f"{qid}-{i}", label, cat, BRANDS[q % 4] if i % 4 else BRANDS[(q + 1) % 4],
                            round(median * (0.4 + i / 10), 2)) for i, label in enumerate(LABEL_MIX)]
    return Catalog.build(queries, judged, CATS)


def test_brand_shoppers_search_where_their_brand_shows_up():
    catalog = brand_specific_catalog()
    lift = intent_lift(list(simulate(catalog, SIM)), catalog.judged)
    # Without brand-aware query choice about a quarter of shown products would match the intent brand.
    assert lift["brand_match_shown"] > 0.45
    assert lift["brand_match_clicked"] > lift["brand_match_shown"]


def test_intent_brands_come_from_the_category_head():
    catalog = brand_specific_catalog()
    for names, _ in catalog.brands.values():
        assert len(names) <= TOP_BRANDS


def test_intent_lift_problems():
    ok = {"brand_match_shown": 0.5, "brand_match_clicked": 0.7, "price_gap_shown": 0.6, "price_gap_clicked": 0.4}
    assert intent_lift_problems(ok) == []
    assert intent_lift_problems({**ok, "brand_match_shown": 0.003, "brand_match_clicked": 0.005})[0] \
        .startswith("intent brand is rarely shown")
    assert intent_lift_problems({**ok, "brand_match_clicked": 0.55}) == ["clicks barely favour the intent brand"]
    assert intent_lift_problems({**ok, "price_gap_clicked": 0.55}) == ["clicks barely favour the intent price"]


def test_simulated_catalogs_have_learnable_intent():
    for catalog in (tiny_catalog(), brand_specific_catalog()):
        assert intent_lift_problems(intent_lift(list(simulate(catalog, SIM)), catalog.judged)) == []


def test_event_ids_are_unique_and_stable(events):
    ids = [e["event_id"] for e in events]
    assert len(ids) == len(set(ids))
    assert all(e["event_id"].startswith(e["session_id"] + "-") for e in events)


def test_event_times_have_millisecond_precision(events):
    # Kafka payloads carry milliseconds; sub-ms label times would let an event count in its own features.
    assert all(e["event_time"].microsecond % 1000 == 0 for e in events)
