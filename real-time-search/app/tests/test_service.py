import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from rts_app.service import (index_query, latency_stats, play_scenario, qac, rows_to_dicts, scenario_timeline, search,
                             expected_session_features, freshness_breakdown, l2_inputs, landed, time_calls,
                             user_feature_values, with_timeout, RecentValues)
from rts_app.service import persona_lift, ranked_top
from rts_shared.events import EVENT_FIELDS

TABLE = {
    "ca": [{"rank": 1, "completion": "camera"}, {"rank": 2, "completion": "camera bag"}, {"rank": 3, "completion": "cat food"}],
    "camera bag f": [{"rank": 1, "completion": "camera bag for sony"}, {"rank": 2, "completion": "camera bag for canon"}],
}


def lookup(prefix):
    return TABLE.get(prefix, [])


def test_qac_returns_l1_and_falls_back_to_it_while_l2_is_not_deployed():
    out = qac("Ca", lookup, max_len=12, n=2)
    assert [c["completion"] for c in out["l1"]] == ["camera", "camera bag"]
    assert out["l2"] == {"status": "not_deployed"}  # the UI shows the L1 order; no duplicate list
    assert out["prefix"] == "ca" and out["timings_ms"]["l1"] >= 0


def test_qac_filters_when_typed_text_is_longer_than_the_key():
    out = qac("camera bag for s", lookup, max_len=12, n=10)
    assert [c["completion"] for c in out["l1"]] == ["camera bag for sony"]


def test_qac_skips_lookup_for_blank_input():
    calls = []
    out = qac("   ", lambda p: calls.append(p) or [], max_len=12, n=10)
    assert out["l1"] == [] and calls == []


class FakeProducer:
    def __init__(self):
        self.sent, self.flushed = [], False

    def produce(self, topic, key, value):
        self.sent.append((topic, key, json.loads(value)))

    def flush(self, timeout=None):
        self.flushed = True
        self.sent.append("flush")
        return 0


SCENARIO = {"scenario_id": "sc1", "user_id": "u42", "events": json.dumps([
    {"offset_ms": 0, "search_id": "s9-0", "event_type": "search", "query_id": 7, "query": "canon lens",
     "product_id": None, "category": "Camera & Photo", "brand": None, "price": None, "device": "desktop"},
    {"offset_ms": 900, "search_id": "s9-0", "event_type": "click", "query_id": 7, "query": "canon lens",
     "product_id": "B01", "category": "Camera & Photo", "brand": "canon", "price": 450.0, "device": "desktop"}])}


NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def produced(p):
    return [x for x in p.sent if x != "flush"]


def test_play_scenario_produces_every_event_under_the_new_session_and_flushes():
    p = FakeProducer()
    sent, _ = play_scenario(SCENARIO, "demo-abc", p, "topic1", now=NOW)
    assert len(sent) == 2 and p.flushed
    assert [(t, k) for t, k, _ in produced(p)] == [("topic1", b"demo-abc")] * 2
    assert all(set(v) == {f for f, _ in EVENT_FIELDS} and v["session_id"] == "demo-abc" for _, _, v in produced(p))
    assert produced(p)[1][2]["user_id"] == "u42" and produced(p)[1][2]["event_type"] == "click"


def test_play_scenario_sends_the_last_event_alone_and_times_only_its_kafka_write():
    p = FakeProducer()
    stamped = datetime(2026, 11, 2, 0, 0, 1, tzinfo=timezone.utc)
    sent, ack_ms = play_scenario(SCENARIO, "demo-abc", p, "topic1", now=NOW, clock=lambda: stamped)
    # The rest is flushed first, untimed; then the last event is stamped at send time and flushed on its own.
    assert [x if x == "flush" else x[2]["event_type"] for x in p.sent] == ["search", "flush", "click", "flush"]
    assert sent[-1]["event_time"] == stamped and produced(p)[-1][2]["event_time"].startswith("2026-11-02T00:00:01")
    assert ack_ms >= 0


def test_play_scenario_ends_on_the_last_click_or_cart():
    # Only a click or cart moves all three session tables, so the timed last event must be one.
    trailing = [{"offset_ms": 1500, "search_id": "s9-0", "event_type": "back", "query_id": 7, "query": "canon lens",
                 "product_id": None, "category": None, "brand": None, "price": None, "device": "desktop"},
                {"offset_ms": 2400, "search_id": "s9-1", "event_type": "search", "query_id": 8, "query": "tripod",
                 "product_id": None, "category": "Camera & Photo", "brand": None, "price": None, "device": "desktop"}]
    scenario = {**SCENARIO, "events": json.dumps(json.loads(SCENARIO["events"]) + trailing)}
    sent, _ = play_scenario(scenario, "demo-abc", FakeProducer(), "topic1", now=NOW)
    assert [e["event_type"] for e in sent] == ["search", "click"]


def test_scenario_timeline_shows_what_was_replayed_relative_to_the_last_event():
    sent, _ = play_scenario(SCENARIO, "demo-abc", FakeProducer(), "topic1", now=NOW, clock=lambda: NOW)
    assert scenario_timeline(sent) == [
        {"t_s": -0.9, "event_type": "search", "query": "canon lens", "product_id": None, "category": "Camera & Photo",
         "brand": None, "price": None},
        {"t_s": 0.0, "event_type": "click", "query": "canon lens", "product_id": "B01", "category": "Camera & Photo",
         "brand": "canon", "price": 450.0}]


VEC = [0.1, 0.2]
HITS = [{"product_id": "B1", "title": "usb c cable", "score": 0.9}, {"product_id": "B2", "title": "usb hub", "score": 0.8}]


def test_hybrid_query_sends_text_for_keywords_and_our_vector_for_ann():
    kw = index_query("usb c", VEC, "HYBRID", k=200, query_columns=["search_text"])
    assert kw == {"query_text": "usb c", "query_vector": VEC, "query_type": "HYBRID", "num_results": 200,
                  "query_columns": ["search_text"]}


def test_ann_query_sends_only_the_vector():
    # The index rejects ANN requests with both: "Only one of Field 'query_text' or Field 'query_vector'".
    kw = index_query("usb c", VEC, "ANN", k=200, query_columns=["search_text"])
    assert kw == {"query_vector": VEC, "query_type": "ANN", "num_results": 200}


def test_without_a_vector_the_index_embeds_the_text_itself():
    assert index_query("usb c", None, "ANN", k=50, query_columns=[]) == {
        "query_text": "usb c", "query_type": "ANN", "num_results": 50}
    assert "query_vector" not in index_query("usb c", None, "HYBRID", k=50, query_columns=["search_text"])


def test_rows_to_dicts_names_columns_from_the_manifest():
    assert rows_to_dicts(["product_id", "title", "score"], [["B1", "usb c cable", 0.9]]) == [HITS[0]]
    assert rows_to_dicts(["product_id"], None) == []


def test_search_embeds_on_our_endpoint_then_queries_the_index():
    calls = []
    out = search(" USB C ", embed=lambda t: calls.append(("embed", t)) or VEC,
                 query_index=lambda **kw: calls.append(("index", kw)) or HITS, query_type="HYBRID", k=200,
                 query_columns=["search_text"])
    assert calls[0] == ("embed", "USB C") and calls[1][1]["query_vector"] == VEC
    assert out["l1"] == HITS and out["l2"] == {"status": "not_deployed"}
    assert out["embedding"] == "custom" and out["query_type"] == "HYBRID"
    assert set(out["timings_ms"]) == {"embed", "search"}


def test_search_falls_back_to_managed_embedding_when_our_endpoint_fails():
    sent = {}

    def broken(_):
        raise TimeoutError("rts-query-embedding")

    out = search("usb c", embed=broken, query_index=lambda **kw: sent.update(kw) or HITS, query_type="HYBRID",
                 k=200, query_columns=["search_text"])
    assert "query_vector" not in sent and sent["query_text"] == "usb c"
    assert out["embedding"] == "fallback_managed" and out["l1"] == HITS


def test_search_skips_both_calls_for_blank_input():
    out = search("  ", embed=lambda t: pytest.fail("embedded"), query_index=lambda **kw: pytest.fail("searched"),
                 query_type="HYBRID", k=200, query_columns=[])
    assert out["l1"] == [] and out["embedding"] == "skipped"


HITS4 = [{"product_id": f"B{i}", "title": t, "score": s}
         for i, (t, s) in enumerate([("usb c cable", 0.9), ("usb hub", 0.8), ("usb c charger", 0.7), ("usb drive", 0.6)], 1)]


def _search(rerank, n=10, hits=HITS4):
    return search("usb", embed=lambda t: VEC, query_index=lambda **kw: hits, query_type="HYBRID", k=200,
                  query_columns=["search_text"], rerank=rerank, n=n)


def test_search_l2_reranks_every_hit_and_shows_the_top_n_of_both():
    sent = []
    out = _search(lambda hits: sent.append(hits) or [0.1, 0.2, 0.3, 0.9], n=2)
    assert sent[0] == HITS4  # all L1 hits, in L1 order
    assert out["hits"] == 4 and [h["product_id"] for h in out["l1"]] == ["B1", "B2"]
    assert out["l2"]["status"] == "ok"
    assert [(h["product_id"], h["l1_rank"]) for h in out["l2"]["items"]] == [("B4", 4), ("B3", 3)]  # UI badges
    assert all("l1_rank" not in h for h in out["l1"])
    assert set(out["timings_ms"]) == {"embed", "search", "l2"}


def test_search_l2_timeout_and_error_fall_back_to_l1_without_items():
    def slow(hits):
        raise TimeoutError

    def broken(hits):
        raise RuntimeError("endpoint 503")

    for rerank, status in [(slow, "timeout"), (broken, "error")]:
        out = _search(rerank)
        assert out["l2"] == {"status": status} and out["l1"] == HITS4 and "l2" in out["timings_ms"]


def test_search_l2_is_not_called_without_hits():
    out = _search(lambda hits: pytest.fail("called"), hits=[])
    assert out["l2"] == {"status": "ok", "items": []}


RANKED = {"ca": [{"rank": i, "completion": c} for i, c in enumerate(["camera", "camera bag", "cat food", "car seat"], 1)]}


def test_qac_l2_reranks_the_top_candidates_and_shows_the_top_n():
    sent = []
    scores = {"camera": 0.1, "camera bag": 0.9, "cat food": 0.5}
    out = qac("ca", lambda p: RANKED.get(p, []), max_len=12, n=2, candidates=3,
              rerank=lambda rows: sent.append(rows) or [scores[r["completion"]] for r in rows])
    assert [r["completion"] for r in sent[0]] == ["camera", "camera bag", "cat food"]  # L1 top 3 only
    assert out["l2"]["status"] == "ok"
    assert [c["completion"] for c in out["l2"]["items"]] == ["camera bag", "cat food"]
    assert [c["completion"] for c in out["l1"]] == ["camera", "camera bag"]
    assert out["timings_ms"]["l2"] >= 0


def test_qac_l2_items_carry_their_l1_position_for_the_ui_badges():
    out = qac("ca", lambda p: RANKED.get(p, []), max_len=12, n=3, candidates=4,
              rerank=lambda rows: [0.1, 0.2, 0.3, 0.9])
    assert [(c["completion"], c["l1_rank"]) for c in out["l2"]["items"]] == [("car seat", 4), ("cat food", 3),
                                                                            ("camera bag", 2)]
    assert all("l1_rank" not in c for c in out["l1"])  # L1 rows are not mutated


def test_qac_l1_rank_is_the_position_after_filtering_not_the_table_rank():
    out = qac("camera bag for c", lookup, max_len=12, n=10, rerank=lambda rows: [1.0] * len(rows))
    assert [(c["completion"], c["rank"], c["l1_rank"]) for c in out["l2"]["items"]] == [("camera bag for canon", 2, 1)]


def test_qac_l2_timeout_and_error_fall_back_to_l1_without_items():
    def slow(rows):
        raise TimeoutError

    def broken(rows):
        raise RuntimeError("endpoint 503")

    for rerank, status in [(slow, "timeout"), (broken, "error")]:
        out = qac("ca", lambda p: RANKED.get(p, []), max_len=12, n=2, candidates=3, rerank=rerank)
        assert out["l2"] == {"status": status} and len(out["l1"]) == 2
        assert "l2" in out["timings_ms"]


def test_qac_l2_is_not_called_without_candidates():
    out = qac("zz", lambda p: [], max_len=12, n=2, candidates=3, rerank=lambda rows: pytest.fail("called"))
    assert out["l2"] == {"status": "ok", "items": []}


def test_with_timeout_returns_fast_results_and_raises_on_slow_ones():
    pool = ThreadPoolExecutor(max_workers=2)
    assert with_timeout(pool, lambda: [1.0], timeout_s=1) == [1.0]
    with pytest.raises(TimeoutError):
        with_timeout(pool, lambda: time.sleep(0.2), timeout_s=0.01)


def test_latency_stats_reports_nearest_rank_percentiles():
    stats = latency_stats([float(x) for x in range(1, 101)])
    assert stats == {"n": 100, "min": 1.0, "p50": 50.0, "p95": 95.0, "p99": 99.0, "max": 100.0}


def test_time_calls_runs_n_times_after_warmup_and_returns_ms():
    calls = []
    times = time_calls(lambda: calls.append(1), n=5, warmup=2)
    assert len(calls) == 7 and len(times) == 5 and all(t >= 0 for t in times)


def test_time_calls_idles_before_each_call_without_timing_the_gap():
    # Typing pace: the connection sits idle between keystrokes; only the call itself is timed.
    started = time.perf_counter()
    times = time_calls(lambda: None, n=4, warmup=0, gap_ms=20)
    assert (time.perf_counter() - started) * 1000 >= 4 * 20
    assert all(t < 5 for t in times)


T0 = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)


def ms(n):
    return T0 + timedelta(milliseconds=n)


EVENTS = [
    {"event_type": "search", "category": "Electronics", "brand": None, "price": None},
    {"event_type": "click", "category": "Electronics", "brand": "Sony", "price": 99.0},
    {"event_type": "add_to_cart", "category": "Electronics", "brand": "Sony", "price": 99.0},
    {"event_type": "click", "category": "Toys", "brand": "Lego", "price": 20.0},
    {"event_type": "back", "category": None, "brand": None, "price": None},
]


def test_expected_session_features_match_the_online_tables():
    assert expected_session_features(EVENTS) == {
        "window": {"sess_n_searches_30m": 1, "sess_n_clicks_30m": 2, "sess_n_carts_30m": 1},
        "category": {"Electronics": 2, "Toys": 1},  # clicks + carts per category
        # The online table holds the last click or cart for every column, event type included, although
        # feature_specs defines sess_last_event_type over all events (observed 2026-11-03; flagged to data/ml).
        "last": {"sess_last_event_type": "click", "sess_last_brand": "Lego", "sess_last_category": "Toys",
                 "sess_last_price": 20.0},
    }


WINDOW = {"sess_n_searches_30m": 1, "sess_n_clicks_30m": 2, "sess_n_carts_30m": 1}
FRESH = {"event_time": T0, "commit_time": ms(41)}  # this row came from the timed last event (stamped T0)


def test_landed_needs_every_replayed_event_in_each_table():
    exp = expected_session_features(EVENTS)
    assert landed("window", exp, [{**WINDOW, **FRESH}], T0)
    assert not landed("window", exp, [{**WINDOW, **FRESH, "sess_n_clicks_30m": 1}], T0)
    assert not landed("window", exp, [], T0)
    assert landed("category", exp, [{"category": "Toys", "sess_cat_cnt_30m": 1, **FRESH},
                                    {"category": "Electronics", "sess_cat_cnt_30m": 2, "event_time": ms(-9000),
                                     "commit_time": ms(-500)}], T0)
    assert not landed("category", exp, [{"category": "Electronics", "sess_cat_cnt_30m": 2, **FRESH}], T0)
    last = {"sess_last_event_type": "click", "sess_last_brand": "Lego", "sess_last_category": "Toys",
            "sess_last_price": 20.000001, "commit_time": ms(44)}
    assert landed("last", exp, [last], T0)
    assert not landed("last", exp, [{**last, "sess_last_brand": "Sony"}], T0)


def test_landed_needs_the_row_written_for_the_timed_event():
    # Same values, but written for an earlier event: its timestamps would not time the last event.
    exp = expected_session_features(EVENTS)
    assert not landed("window", exp, [{**WINDOW, "event_time": ms(-2000), "commit_time": ms(-20)}], T0)
    assert not landed("category", exp, [{"category": "Toys", "sess_cat_cnt_30m": 1, "event_time": ms(-2000),
                                         "commit_time": ms(-20)},
                                        {"category": "Electronics", "sess_cat_cnt_30m": 2, **FRESH}], T0)
    assert not landed("last", exp, [{"sess_last_event_type": "click", "sess_last_brand": "Lego",
                                     "sess_last_category": "Toys", "sess_last_price": 20.0, "commit_time": ms(-20)}], T0)


def test_freshness_breakdown_stages_end_at_timestamps_and_never_go_negative():
    # The app's ack can arrive after the pipeline already emitted the row (the ack's return trip, clock skew).
    seen = {k: {"rows": [{**WINDOW, "event_time": T0, "output_time": ms(30), "commit_time": ms(90),
                          "category": "Toys", "sess_cat_cnt_30m": 1}], "detected_at": ms(112)}
            for k in ("window", "category", "last")}
    out = freshness_breakdown(seen, last_event_time=T0, kafka_ack_ms=55.0)
    assert [g["ms"] for g in out["stages"]] == [55.0, 0.0, 35.0, 22.0]
    assert sum(g["ms"] for g in out["stages"]) == out["total_ms"] == 112.0


def test_freshness_breakdown_splits_the_clicks_feature_into_stages_and_times_every_feature():
    seen = {
        "window": {"rows": [{**WINDOW, "event_time": T0, "output_time": ms(30), "commit_time": ms(41)}],
                   "detected_at": ms(57)},
        "category": {"rows": [{"category": "Electronics", "sess_cat_cnt_30m": 2, "event_time": ms(-9000),
                               "output_time": ms(28), "commit_time": ms(38)},
                              {"category": "Toys", "sess_cat_cnt_30m": 1, "event_time": ms(-2000),
                               "output_time": ms(31), "commit_time": ms(42)}],
                     "detected_at": ms(54)},
        "last": {"rows": [{"sess_last_event_type": "click", "commit_time": ms(44)}], "detected_at": ms(60)},
    }
    out = freshness_breakdown(seen, last_event_time=T0, kafka_ack_ms=3.0)
    assert out["total_ms"] == 60.0  # until the app had read every table
    assert out["stages"] == [
        {"name": "Kafka write", "source": "app", "ms": 3.0},
        {"name": "Real-Time Mode read + computed", "source": "pipeline · output_time", "ms": 27.0},
        {"name": "Lakebase write", "source": "pipeline · commit_time", "ms": 11.0},
        {"name": "App read every feature", "source": "app · Lakebase", "ms": 19.0},  # stages add up to the total
    ]
    by_name = {f["feature"]: f for f in out["features"]}
    assert by_name["sess_n_clicks_30m"] == {"feature": "sess_n_clicks_30m", "table": "window", "value": 2,
                                           "in_lakebase_ms": 41.0, "served_ms": 57.0}
    # Replayed events keep their simulated (past) event_time but are all produced at Play, so every offset is from
    # the last event, the only one whose event_time is the real produce time; never from a row's own event_time.
    assert by_name["sess_cat_cnt_30m[Toys]"]["in_lakebase_ms"] == 42.0
    assert by_name["sess_cat_cnt_30m[Electronics]"]["served_ms"] == 54.0
    assert by_name["sess_last_event_type"]["in_lakebase_ms"] == 44.0


def test_category_rows_from_earlier_events_have_no_offset():
    # Committed while the untimed rest of the replay was sent: before the timed event, so nothing to time.
    seen = {k: {"rows": [{**WINDOW, "event_time": T0, "output_time": ms(30), "commit_time": ms(41)}],
                "detected_at": ms(57)} for k in ("window", "last")}
    seen["category"] = {"rows": [{"category": "Toys", "sess_cat_cnt_30m": 1, "event_time": T0, "commit_time": ms(42)},
                                 {"category": "Electronics", "sess_cat_cnt_30m": 2, "event_time": ms(-9000),
                                  "commit_time": ms(-480)}], "detected_at": ms(54)}
    by_name = {f["feature"]: f for f in freshness_breakdown(seen, T0, 3.0)["features"]}
    assert by_name["sess_cat_cnt_30m[Electronics]"]["in_lakebase_ms"] is None
    assert by_name["sess_cat_cnt_30m[Electronics]"]["served_ms"] is None
    assert by_name["sess_cat_cnt_30m[Toys]"]["in_lakebase_ms"] == 42.0


def test_recent_values_keeps_the_newest_and_forgets_the_oldest():
    times = RecentValues(max_size=2)
    times.record("a", 41.0)
    times.record("b", 52.5)
    times.record("c", 60.0)
    assert times.get("a") is None and times.get("b") == 52.5 and times.get("c") == 60.0



CFG = {"feature_store": {"session_window_minutes": 30, "user_window_days": 30, "item_window_days": 7}}


def test_l2_inputs_group_what_the_qac_endpoint_receives_by_source():
    groups = l2_inputs(CFG)
    assert [len(groups[k]) for k in ("stream", "batch", "request")] == [12, 4, 13]
    assert all(f.startswith("sess_") for f in groups["stream"]) and all(f.startswith("user_") for f in groups["batch"])
    assert "l1_rank" in groups["request"] and "is_guest" in groups["request"]
    assert not any(f.startswith("item_") for g in groups.values() for f in g)  # QAC L2 doesn't read item features


def test_user_feature_values_drop_keys_and_list_categories_by_count():
    rows = [[{"user_id": "u1", "user_n_clicks_30d": 8}],
            [{"user_id": "u1", "category": "Toys", "user_cat_cnt_30d": 1},
             {"user_id": "u1", "category": "Pet Supplies", "user_cat_cnt_30d": 5}],
            []]  # a user with no carts has no row
    assert user_feature_values(rows) == [{"feature": "user_n_clicks_30d", "value": 8},
                                         {"feature": "user_cat_cnt_30d[Pet Supplies]", "value": 5},
                                         {"feature": "user_cat_cnt_30d[Toys]", "value": 1}]


def test_ranked_top_orders_by_score_and_keeps_ties_in_l1_order():
    items = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
    assert [r["id"] for r in ranked_top(items, [0.1, 0.9, 0.9, 0.5], k=3)] == ["b", "c", "d"]


def test_persona_lift_compares_category_share_in_the_top_results():
    cat = {"category": "Electronics"}
    other = {"category": "Books & Media"}
    guest = [[cat, other, other, other], [other, other, other, other]]   # 1 of 8
    persona = [[cat, cat, other, other], [cat, other, other, other]]     # 3 of 8
    assert persona_lift(guest, persona, "Electronics", min_lift=2.0) == {"guest": 0.125, "persona": 0.375, "ok": True}
    assert persona_lift(guest, guest, "Electronics", min_lift=2.0)["ok"] is False   # no lift
    none = [[other], [other]]
    assert persona_lift(none, none, "Electronics", min_lift=2.0)["ok"] is False      # 0 -> 0 is not a lift
